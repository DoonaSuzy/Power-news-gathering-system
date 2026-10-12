<script setup>
/**
 * 应用主组件：把「数据（composable）— AI 层 — 视图」串起来。
 *
 * 三条数据流：
 * 1. **列表**：``useNewsFeed`` 管筛选与分页；
 * 2. **公告**：``useAiNotices`` 调 ``api/ai.js``（中台 AI 或本地规则）；
 * 3. **调度**：``usePolling`` 每 2 小时把上面两者一起刷新。
 *
 * 为什么定时任务刷新「列表 + 公告」而不是只刷列表：
 * 公告是对同一批新闻的解读，列表更新了而公告还停留在上一批，
 * 用户会看到「公告说储能 11 篇，列表里一条储能的都没有」。
 */
import { computed, onMounted, ref, watch } from 'vue'
import FilterBar from './components/FilterBar.vue'
import NoticeBoard from './components/NoticeBoard.vue'
import NewsList from './components/NewsList.vue'
import LiveNews from './components/LiveNews.vue'
import ArticleDetail from './components/ArticleDetail.vue'
import { useNewsFeed } from './composables/useNewsFeed.js'
import { useAiNotices } from './composables/useAiNotices.js'
import { usePolling } from './composables/usePolling.js'
import { fetchHealth, fetchProvinces } from './api/news.js'
import { getToken, setToken } from './api/client.js'
import { RANGE_PRESETS } from './utils/topics.js'

const INTERVAL_MS = Number(import.meta.env.VITE_POLL_INTERVAL_MS) || 2 * 60 * 60 * 1000

const feed = useNewsFeed()
const board = useAiNotices()

const provinces = ref([])
const health = ref(null)
const healthError = ref('')
const selected = ref(null)
const showSettings = ref(false)
const tokenInput = ref(getToken())
const lastResult = ref('')

const windowLabel = computed(() => {
  const found = RANGE_PRESETS.find((r) => r.key === feed.filters.range)
  return found ? found.label : '自定义区间'
})

/**
 * 本地采集库最新一条的日期。
 *
 * 用来在「实时检索」区块旁显示「本地库最新到 X 号」—— 用户一眼就知道
 * 本地库有没有断更，而不是自己一行行翻列表猜。
 */
const latestLocalDate = computed(() => {
  const dates = feed.items.value
    .map((a) => String(a.publishedAt || '').slice(0, 10))
    .filter(Boolean)
  return dates.length ? dates.reduce((a, b) => (a > b ? a : b)) : ''
})

/** AI 层需要的上下文：当前筛选 + 参与解读的文章 + 时间窗口描述。 */
function aiContext() {
  return {
    articles: feed.items.value,
    filters: {
      province: feed.filters.province || feed.filters.region,
      category: feed.filters.category,
      keyword: feed.filters.keyword,
    },
    window: { range: feed.filters.range, label: windowLabel.value },
  }
}

/** 一次完整刷新：列表 → 公告。公告必须用**刷新后**的列表，否则慢一拍。 */
async function refreshAll(reason) {
  const result = await feed.reload(reason)
  await board.refresh(aiContext())
  lastResult.value = `${reason}：${result?.total ?? 0} 条命中`
  return result
}

const polling = usePolling(refreshAll, {
  intervalMs: INTERVAL_MS,
  key: 'news-feed',
})

async function checkHealth() {
  healthError.value = ''
  try {
    health.value = await fetchHealth()
  } catch (err) {
    health.value = null
    healthError.value = err?.message || String(err)
  }
}

onMounted(async () => {
  await checkHealth()
  try {
    provinces.value = await fetchProvinces()
  } catch (err) {
    healthError.value = err?.message || String(err)
  }
  // 首次数据由轮询调度负责（runOnStart），这里不重复请求
  polling.start()
})

/**
 * 筛选变更立即重拉。
 *
 * 防抖 250ms：关键词输入与日期选择会连续触发，不防抖会打出一串请求，
 * 而竞态令牌只能保证「显示的是最新的」，省不掉中台的无效查询。
 */
let debounce = null
watch(
  () => ({ ...feed.filters }),
  () => {
    if (debounce) clearTimeout(debounce)
    debounce = setTimeout(() => {
      feed.reload('filter').then(() => board.refresh(aiContext())).catch(() => {})
    }, 250)
  },
  { deep: true },
)

function saveToken() {
  setToken(tokenInput.value.trim())
  showSettings.value = false
  checkHealth()
  refreshAll('token').catch(() => {})
}

const healthOk = computed(() => {
  if (!health.value) return false
  return (health.value.dependencies || []).every((d) => d.ok)
})

const degradedDeps = computed(() =>
  (health.value?.dependencies || []).filter((d) => !d.ok).map((d) => d.name),
)

/**
 * 点公告条目 → 打开对应文章。
 *
 * 公告引用的是**整个窗口**里选出的文章 id，而列表只有当前页 20 条，
 * 找不到是常态。此时如实提示，而不是静默无反应 —— 静默会让用户
 * 以为「点了没反应是 bug」。
 */
function openNoticeArticles(ids) {
  const found = feed.items.value.find((a) => ids.includes(a.id))
  if (found) {
    selected.value = found
    return
  }
  const page = Math.floor((feed.total.value || 0) / feed.pageSize) + 1
  lastResult.value = `该要点引用的文章不在此页（id ${ids.join('、')}），可翻页或点「加载更多」查找（共 ${page} 页）`
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <strong>电力新闻资讯</strong>
        <span class="muted">Power News Aggregator</span>
      </div>

      <div class="status">
        <span class="tag" :class="healthOk ? 'ok' : 'danger'" :title="health ? '中台依赖状态' : healthError">
          {{ health ? (healthOk ? '中台正常' : `降级：${degradedDeps.join('/')}`) : '中台不可达' }}
        </span>
        <span class="tag primary">公告：{{ board.ready.value ? board.methodLabel.value : '生成中' }}</span>
        <span class="tag" :title="`每 ${INTERVAL_MS / 3600000} 小时自动更新`">
          <span v-if="polling.running.value" class="spinner" />
          <span v-else>下次更新 {{ polling.countdownText.value }}</span>
        </span>
        <span v-if="polling.lastError.value" class="tag danger" :title="polling.lastError.value">上次失败</span>
      </div>

      <div class="actions">
        <button :disabled="polling.running.value" @click="polling.runNow()">
          {{ polling.running.value ? '刷新中…' : '立即刷新' }}
        </button>
        <button @click="showSettings = !showSettings">设置</button>
      </div>
    </header>

    <div v-if="showSettings" class="settings">
      <label for="api-token">
        中台 API Key（Bearer Token）
        <input id="api-token" v-model="tokenInput" name="api-token" placeholder="pn_... 或引导令牌" />
      </label>
      <button class="primary" @click="saveToken">保存并重连</button>
      <span class="muted">
        脚本可用 <code class="mono">.workbuddy/e2e_token</code> 里缓存的值；
        也可用 <code class="mono">.env</code> 里的引导令牌
        <code class="mono">POWERNEWS_PLATFORM_BOOTSTRAP_ADMIN_TOKEN</code>（仅库里没有 Key 时有效）。
      </span>
    </div>

    <p v-if="healthError" class="banner error global">
      中台连接失败：{{ healthError }}
    </p>

    <main class="layout">
      <aside class="left">
        <FilterBar
          :filters="feed.filters"
          :provinces="provinces"
          :active-count="feed.activeFilterCount.value"
          @patch="(k, v) => (feed.filters[k] = v)"
          @reset="feed.reset()"
          @search="(v) => (feed.filters.keyword = v)"
        />

        <NoticeBoard
          :notices="board.notices.value"
          :loading="board.loading.value"
          :method="board.method.value"
          :caveats="board.caveats.value"
          :updated-at="0"
          :generated-at="board.generatedAt.value"
          :next-refresh-at="board.nextRefreshAt.value"
          :source-summary="board.sourceSummary.value"
          :ready="board.ready.value"
          @open-articles="openNoticeArticles"
        />

        <div class="diag">
          <div class="diag-row">
            <span class="muted">定时任务</span>
            <span>每 {{ INTERVAL_MS / 3600000 }} 小时 · 已执行 {{ polling.runCount.value }} 次</span>
          </div>
          <div class="diag-row">
            <span class="muted">上次执行</span>
            <span>{{ polling.lastSuccessAt.value ? new Date(polling.lastSuccessAt.value).toLocaleString('zh-CN') : '尚未执行' }}</span>
          </div>
          <div class="diag-row">
            <span class="muted">连续失败</span>
            <span :class="{ danger: polling.failureCount.value > 0 }">{{ polling.failureCount.value }}</span>
          </div>
          <div v-if="lastResult" class="diag-row">
            <span class="muted">最近一次</span>
            <span>{{ lastResult }}</span>
          </div>
        </div>
      </aside>

      <div class="right">
        <LiveNews :latest-local-date="latestLocalDate" />
        <NewsList
          :items="feed.items.value"
          :loading="feed.loading.value"
          :loading-more="feed.loadingMore.value"
          :has-more="feed.hasMore.value"
          :total="feed.total.value"
          @open="(a) => (selected = a)"
          @load-more="feed.loadMore()"
        />
      </div>
    </main>

    <ArticleDetail v-if="selected" :article="selected" @close="selected = null" />
  </div>
</template>

<style scoped>
.app {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 14px 14px;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 9px 14px;
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.brand strong { font-size: 16px; }

.status {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  flex: 1;
}

.actions { display: flex; gap: 6px; }

.settings {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  background: var(--c-primary-soft);
  border: 1px solid #c7d7fe;
  border-radius: var(--radius);
  padding: 8px 12px;
  font-size: 12px;
}

.settings label {
  display: flex;
  align-items: center;
  gap: 6px;
}

.settings input { width: 320px; }

.global { margin: 0; }

.layout {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(360px, 460px) 1fr;
  gap: 10px;
  min-height: 0;
}

.left {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  overflow-y: auto;
}

/* 右列：实时检索区块（可折叠）+ 主列表。列表要占满剩余高度，
   所以这里用 flex 而不是让两者各占自然高度。 */
.right {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.right > :last-child {
  flex: 1;
  min-height: 0;
}

.diag {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 9px 12px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.diag-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}

.danger { color: var(--c-danger); }

@media (max-width: 1080px) {
  .layout { grid-template-columns: 1fr; }
}
</style>
