/**
 * AI 公告栏数据。
 *
 * ## 数据来自服务端缓存，**不在前端调用 LLM**
 *
 * 改造前：进入页面 → 前端调 LLM 实时生成。三个后果：
 * 1. token 消耗与访客数成正比（每个访客每次打开都算一次）；
 * 2. 不同访客看到的公告内容不同（模型有随机性），而「公告栏」本该是同一份公告；
 * 3. 前端定时器只在页面开着时跑，关掉页面就不再刷新。
 *
 * 现在：服务端启动时预生成一次、之后每 2 小时刷新，前端 `GET /api/v1/notices`
 * 只读缓存 —— **LLM 调用次数与访客量完全解耦**，且所有人看到同一份公告。
 *
 * 手动刷新按钮仍然保留（`refreshRemote`），它触发服务端重新生成，
 * 属于显式操作，不是自动行为。
 *
 * @module composables/useAiNotices
 */
import { computed, ref } from 'vue'
import { apiGet } from '../api/client.js'

const ENDPOINT = '/notices'

export function useAiNotices() {
  const notices = ref([])
  const keyPoints = ref([])
  const highlights = ref([])
  const headline = ref('')
  /** 服务端缓存是否已就绪（服务刚启动时预热未完成 → false）。 */
  const ready = ref(false)
  /** 缓存生成时刻（ISO 字符串，由服务端给出）。 */
  const generatedAt = ref('')
  /** 下次服务端刷新时刻。 */
  const nextRefreshAt = ref('')
  /** 'llm' | 'title-only' | 'none' */
  const method = ref('')
  /** 数据来源构成：本地采集篇数 / 检索条数。 */
  const sources = ref({})
  const caveats = ref([])
  const loading = ref(false)
  const error = ref('')

  const pinned = computed(() => notices.value.filter((n) => n.pinned))
  const rest = computed(() => notices.value.filter((n) => !n.pinned))
  const isEmpty = computed(() => !loading.value && notices.value.length === 0)

  /** 数据来源的一行摘要，用于界面展示「内容从哪来」。 */
  const sourceSummary = computed(() => {
    const local = sources.value?.local_articles ?? 0
    const search = sources.value?.search_docs ?? 0
    if (!local && !search) return ''
    return `本地采集 ${local} 篇 + 实时检索 ${search} 条`
  })

  const METHOD_LABEL = {
    llm: 'AI 模型',
    'title-only': '仅标题',
    none: '不可用',
  }
  const methodLabel = computed(() => METHOD_LABEL[method.value] || method.value || '未知')

  /**
   * 读缓存。
   * @param {object} ctx 保留参数签名（调用方仍传 articles/window），
   *   但**已不再使用** —— 数据由服务端按自己的窗口生成，与前端筛选解耦。
   *   这与需求 1 一致：公告是「每天的公告」，不该随访客的筛选变化。
   */
  async function refresh(ctx = {}) {
    void ctx
    loading.value = true
    error.value = ''
    try {
      const data = await apiGet(ENDPOINT)
      notices.value = data?.notices || []
      keyPoints.value = data?.key_points || []
      highlights.value = data?.highlights || []
      headline.value = data?.headline || ''
      ready.value = Boolean(data?.ready)
      generatedAt.value = data?.generated_at || ''
      nextRefreshAt.value = data?.next_refresh_at || ''
      method.value = data?.method || ''
      sources.value = data?.sources || {}
      caveats.value = data?.caveats || []
      if (!ready.value && data?.reason) caveats.value = [data.reason, ...caveats.value]
      return data
    } catch (err) {
      error.value = err?.message || String(err)
      throw err
    } finally {
      loading.value = false
    }
  }

  return {
    notices, keyPoints, highlights, headline,
    ready, generatedAt, nextRefreshAt, method, sources, caveats,
    loading, error, pinned, rest, isEmpty, sourceSummary, methodLabel,
    refresh,
  }
}
