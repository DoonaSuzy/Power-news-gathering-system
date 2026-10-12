/**
 * 周期任务调度：每 N 分钟稳定跑一次（默认 2 小时）。
 *
 * ## 为什么不用裸 setInterval
 *
 * 裸 ``setInterval(task, 7200000)`` 在真实页面里有三个必然失效场景：
 *
 * 1. **后台标签页被节流**。浏览器会把后台 tab 的定时器压到 ≥1 分钟，
 *    更久甚至完全冻结 —— 冻结期间不会「补跑」，任务就这么漏掉了；
 * 2. **页面重开即失忆**。用户关掉页面 3 小时后打开，interval 从头开始计时，
 *    再等 2 小时才刷新，而数据其实早就该更新了；
 * 3. **失败就断链**。一次网络抖动让任务失败，下一次要等满整个周期。
 *
 * ## 本实现的做法
 *
 * 用一个 **1 秒的心跳 tick + 绝对时刻 ``nextRunAt``** 来驱动，而不是靠定时器
 * 自己数满周期：
 *
 * - 心跳被节流/冻结没关系，恢复后第一次 tick 就会看到 ``now >= nextRunAt``
 *   并**立即补跑**（天然处理场景 1）；
 * - ``lastRunAt`` 持久化到 localStorage，页面重开时用「上次运行 + 周期」算出
 *   ``nextRunAt``，已过期就立刻跑（处理场景 2）；
 * - 失败按 30s → 1m → 2m → … 指数退避重试，成功后退避清零（处理场景 3）。
 *
 * 心跳只做一次 ``Date.now()`` 比较，开销可忽略。
 *
 * @module composables/usePolling
 */
import { computed, onUnmounted, ref } from 'vue'

const STORAGE_PREFIX = 'powernews.poll.'
const BACKOFF_BASE = 30_000
const BACKOFF_MAX = 30 * 60_000

function readStored(key) {
  const raw = localStorage.getItem(STORAGE_PREFIX + key)
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : 0
}

function writeStored(key, value) {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, String(value))
  } catch {
    // 隐私模式下 localStorage 可能不可写；失败不影响本次运行，只是丢了跨会话记忆
  }
}

/**
 * @param {() => Promise<any>} task 要周期执行的任务（应为幂等的「重新拉取」）。
 * @param {object} options
 * @param {number} [options.intervalMs] 周期，默认 2 小时。
 * @param {string} [options.key] localStorage 键名，区分不同任务。
 * @param {boolean} [options.runOnStart] 挂载时是否立即跑（默认 true）。
 *   默认值是有意的：挂载时内存里没有数据，不立即跑就是白屏。
 *   只有「数据由别处提供、这里纯做后台刷新」的场景才该设为 false。
 */
export function usePolling(task, options = {}) {
  const intervalMs = options.intervalMs ?? 2 * 60 * 60 * 1000
  const key = options.key ?? 'default'
  const runOnStart = options.runOnStart !== false

  const running = ref(false)
  const lastRunAt = ref(readStored(key))
  const lastSuccessAt = ref(0)
  const lastError = ref('')
  const failureCount = ref(0)
  const runCount = ref(0)
  const now = ref(Date.now())
  /** 下次计划执行的绝对时刻。失败退避也写在这里，调度逻辑只有一处。 */
  const nextRunAt = ref(0)

  let heartbeat = null

  function planNext(delayMs) {
    nextRunAt.value = Date.now() + delayMs
    now.value = Date.now()
  }

  async function run(reason = 'manual') {
    if (running.value) return { skipped: true }
    running.value = true
    lastError.value = ''
    try {
      await task(reason)
      lastRunAt.value = Date.now()
      lastSuccessAt.value = lastRunAt.value
      writeStored(key, lastRunAt.value)
      failureCount.value = 0
      runCount.value += 1
      planNext(intervalMs)
      return { ok: true }
    } catch (err) {
      failureCount.value += 1
      lastError.value = err?.message || String(err)
      // 指数退避但封顶：抖动不该让任务停摆半小时以上
      const delay = Math.min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (failureCount.value - 1))
      planNext(delay)
      return { ok: false, error: lastError.value, retryInMs: delay }
    } finally {
      running.value = false
    }
  }

  function tick() {
    now.value = Date.now()
    if (!running.value && nextRunAt.value && now.value >= nextRunAt.value) {
      run('schedule')
    }
  }

  /** 页面从后台恢复时立刻核一次 —— 这就是「后台被冻结也能补跑」的入口。 */
  function onVisibility() {
    if (document.visibilityState === 'visible') tick()
  }

  function start() {
    if (heartbeat) return
    heartbeat = setInterval(tick, 1000)
    document.addEventListener('visibilitychange', onVisibility)

    const dueAt = lastRunAt.value ? lastRunAt.value + intervalMs : 0
    const overdue = dueAt > 0 && Date.now() >= dueAt

    // ⚠️ 挂载时**必须**拉一次。
    //
    // 曾经的写法是「上次拉取还在周期内就跳过」，结果是：页面重开时内存里
    // 一条数据都没有（localStorage 只存了时间戳，没存数据），而任务被判定为
    // 「还没到点」→ 列表整片空白，用户只能手动点刷新。这个缺陷只在第二次
    // 打开页面时才复现，首轮开发极易漏掉。
    //
    // localStorage 的时间戳只用来区分这次是「常规首拉」还是「补跑过期数据」，
    // 不参与「要不要拉」的判断 —— 要不要拉，取决于内存里有没有数据。
    if (runOnStart) {
      run(overdue ? 'stale' : 'first')
    } else {
      planNext(dueAt > Date.now() ? dueAt - Date.now() : intervalMs)
    }
  }

  function stop() {
    if (heartbeat) clearInterval(heartbeat)
    heartbeat = null
    document.removeEventListener('visibilitychange', onVisibility)
  }

  // 组件卸载必须停：否则离开页面后定时器仍在跑，在 HMR 下会叠加出多个心跳
  onUnmounted(stop)

  const countdownMs = computed(() => Math.max(0, nextRunAt.value - now.value))
  const countdownText = computed(() => {
    const ms = countdownMs.value
    if (!ms) return '即将执行'
    const total = Math.floor(ms / 1000)
    const h = String(Math.floor(total / 3600)).padStart(2, '0')
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
    const s = String(total % 60).padStart(2, '0')
    return `${h}:${m}:${s}`
  })

  return {
    intervalMs,
    running,
    lastRunAt,
    lastSuccessAt,
    lastError,
    failureCount,
    runCount,
    nextRunAt,
    countdownMs,
    countdownText,
    start,
    stop,
    runNow: () => run('manual'),
  }
}
