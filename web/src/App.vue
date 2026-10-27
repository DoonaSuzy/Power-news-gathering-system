<script setup>
/**
 * 应用主组件：接口文档式的查询工具。
 *
 * 三条硬规则（来自设计评审）：
 * 1. **未选省份不发请求**——首屏只有一句引导，不展示任何数据；
 * 2. **省份是唯一必填项**，数据域、时间、关键词都是可选项；
 * 3. **切换省份整体替换**——站点表与文章列表同时刷新，不留上一个省的数据。
 *
 * 为什么去掉了公告栏、实时检索与定时轮询：那是资讯门户的能力，
 * 与「按省查数据」的目标无关，只会让页面变重、默认就把数据摊在眼前。
 */
import { computed, onMounted, ref, watch } from 'vue'
import ProvincePicker from './components/ProvincePicker.vue'
import SiteTable from './components/SiteTable.vue'
import NewsList from './components/NewsList.vue'
import ArticleDetail from './components/ArticleDetail.vue'
import { useNewsFeed } from './composables/useNewsFeed.js'
import {
  fetchChannels,
  fetchHealth,
  fetchProvinceCoverage,
  fetchProvinces,
  fetchSourceStats,
} from './api/news.js'
import { getToken, setToken } from './api/client.js'
import { CHANNEL_LABELS, NATIONWIDE, RANGE_PRESETS, describeFreshness } from './utils/topics.js'
import { formatApiError } from './utils/errors.js'

const feed = useNewsFeed()

const provinces = ref([])
const coverage = ref([])
const channels = ref([])
const sites = ref([])
const sitesLoading = ref(false)
const health = ref(null)
const healthError = ref('')
/** 查询类错误：与 healthError 分开，前者随筛选清除，后者是启动期的连接状态 */
const queryError = ref(null)
const selected = ref(null)
const showSettings = ref(false)
const tokenInput = ref(getToken())
const lastResult = ref('')

const CHANNELS = [
  { value: 'all', label: CHANNEL_LABELS.all },
  { value: 'power', label: CHANNEL_LABELS.power },
  { value: 'province', label: CHANNEL_LABELS.province },
]

const hasProvince = computed(() => Boolean(feed.filters.province))
const isNationwide = computed(() => feed.filters.province === NATIONWIDE)
const provinceName = computed(() => {
  if (isNationwide.value) return '全国'
  return provinces.value.find((p) => p.code === feed.filters.province)?.name || ''
})
const provinceCoverage = computed(
  () => coverage.value.find((c) => c.code === feed.filters.province) || null,
)
const rangeLabel = computed(
  () => RANGE_PRESETS.find((r) => r.key === feed.filters.range)?.label || '该时段',
)
const activeSource = computed(
  () => sites.value.find((s) => s.adapterName === feed.filters.source) || null,
)
const totalSites = computed(() => coverage.value.reduce((n, c) => n + c.sourceCount, 0))

/** 当前数据域对应的一句话说明（列表标题与站点表空态共用）。 */
const channelLabel = computed(() => CHANNEL_LABELS[feed.filters.channel] || '全部新闻')

/** 省份域的「数据有多大年纪」——省级是历史快照，必须如实告知。 */
const freshness = computed(() => {
  const province = channels.value.find((c) => c.channel === 'province')
  return describeFreshness(province?.latestPublishedAt, new Date(), { scope: '省级' })
})

/** 错误提示：查询错误优先于站点错误（前者是这次筛选的直接结果）。 */
const errorInfo = computed(() => (queryError.value ? formatApiError(queryError.value) : null))

/**
 * 空结果分三种情况，文案必须不同——都写「没有新闻」用户分不清是
 * 「这个省没站点」「站点没产出」还是「筛选太窄」。
 */
const emptyHint = computed(() => {
  // 全国范围没有「该省覆盖度」这个概念，沿用省份分支会报出「已配置 N 个站点」
  if (isNationwide.value) {
    return `全国范围内近 ${rangeLabel.value}无符合当前筛选的文章。`
  }
  if (!sites.value.length) return `${provinceName.value}暂无已配置站点。`
  const covered = provinceCoverage.value?.articleCount ?? 0
  if (covered === 0) {
    return `${provinceName.value}已配置 ${sites.value.length} 个站点，但暂无入库文章。`
  }
  return `${provinceName.value}近 ${rangeLabel.value}无符合当前筛选的文章。`
})
const totalArticles = computed(() => coverage.value.reduce((n, c) => n + c.articleCount, 0))

/** 站点概览：选省后才拉，且跟随数据域切换。 */
async function loadSites() {
  if (!hasProvince.value) {
    sites.value = []
    return
  }
  sitesLoading.value = true
  try {
    sites.value = await fetchSourceStats({
      province: feed.filters.province,
      channel: feed.filters.channel,
    })
  } catch (err) {
    sites.value = []
    // 站点表失败也走统一错误态：以前只写进顶栏一行小字，用户基本看不见
    queryError.value = queryError.value || err
  } finally {
    sitesLoading.value = false
  }
}

/** 一次完整刷新：站点表 + 文章列表。未选省份时只清空，不发请求。 */
async function refresh(reason = 'manual') {
  if (!hasProvince.value) {
    feed.clear()
    sites.value = []
    queryError.value = null
    lastResult.value = ''
    return
  }
  queryError.value = null
  try {
    await Promise.all([loadSites(), feed.reload(reason)])
    lastResult.value = `${provinceName.value}：${feed.total.value} 篇 / ${sites.value.length} 个站点`
  } catch (err) {
    // 以前这里是裸 await：reload 一抛就变成 unhandled rejection，
    // 界面上什么都不显示，401 看起来就像「这个省没有新闻」。
    //
    // 分工：鉴权失败给横幅（要带「填写 API Key」的入口，比列表里一行字显眼）；
    // 其余查询错误由 NewsList 就地展示，离筛选条件更近，也不重复打扰。
    queryError.value = err?.isAuthError ? err : null
    lastResult.value = ''
  }
}

/** 横幅上的「重试」：重跑当前筛选，失败也不弹未捕获异常。 */
function retry() {
  refresh('retry')
}

let debounce = null
watch(
  () => ({
    province: feed.filters.province,
    channel: feed.filters.channel,
    source: feed.filters.source,
    keyword: feed.filters.keyword,
    range: feed.filters.range,
  }),
  () => {
    if (debounce) clearTimeout(debounce)
    if (!hasProvince.value) {
      // 清空省份立刻清屏，不等防抖——否则会短暂显示上一个省的数据
      feed.clear()
      sites.value = []
      queryError.value = null
      lastResult.value = ''
      return
    }
    debounce = setTimeout(() => {
      refresh('filter').catch(() => {})
    }, 250)
  },
)

function pickSite(site) {
  feed.filters.source = site.adapterName
}

function clearSource() {
  feed.filters.source = ''
}

function clearProvince() {
  feed.filters.province = ''
}

async function checkHealth() {
  healthError.value = ''
  try {
    health.value = await fetchHealth()
  } catch (err) {
    health.value = null
    healthError.value = err?.message || String(err)
  }
}

function saveToken() {
  setToken(tokenInput.value.trim())
  showSettings.value = false
  checkHealth()
  refresh('token').catch(() => {})
}

const healthOk = computed(
  () => Boolean(health.value) && (health.value.dependencies || []).every((d) => d.ok),
)
const degradedDeps = computed(() =>
  (health.value?.dependencies || []).filter((d) => !d.ok).map((d) => d.name),
)

onMounted(async () => {
  await checkHealth()
  // 三者互不依赖，且失败等级不同：省份字典挂了页面就没法用（要报），
  // /channels 挂了只是少了「数据截止」提示（降级，不报）。
  const [provinceRes, coverageRes, channelRes] = await Promise.allSettled([
    fetchProvinces(),
    fetchProvinceCoverage(),
    fetchChannels(),
  ])
  if (provinceRes.status === 'fulfilled') provinces.value = provinceRes.value
  if (coverageRes.status === 'fulfilled') coverage.value = coverageRes.value
  if (channelRes.status === 'fulfilled') channels.value = channelRes.value

  const bootError = [provinceRes, coverageRes].find((r) => r.status === 'rejected')
  if (bootError) healthError.value = bootError.reason?.message || String(bootError.reason)
  // 首屏**不请求**文章与站点：必须等用户选省
})
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <strong>新闻数据查询</strong>
        <span class="muted">Power News Aggregator</span>
      </div>

      <div class="status">
        <span
          class="tag"
          :class="healthOk ? 'ok' : 'danger'"
          :title="health ? '中台依赖状态' : healthError"
        >
          {{ health ? (healthOk ? '中台正常' : `降级：${degradedDeps.join('/')}`) : '中台不可达' }}
        </span>
        <span v-if="lastResult" class="muted">{{ lastResult }}</span>
      </div>

      <div class="actions">
        <button :disabled="!hasProvince || feed.loading.value" @click="refresh('manual')">
          {{ feed.loading.value ? '查询中…' : '刷新' }}
        </button>
        <button @click="showSettings = !showSettings">设置</button>
      </div>
    </header>

    <div v-if="showSettings" class="settings">
      <label for="api-token">
        中台 API Key
        <input id="api-token" v-model="tokenInput" name="api-token" placeholder="pn_..." />
      </label>
      <button class="primary" @click="saveToken">保存并重连</button>
    </div>

    <p v-if="healthError" class="banner error">{{ healthError }}</p>

    <section class="toolbar">
      <label class="field">
        <span>数据域</span>
        <select v-model="feed.filters.channel">
          <option v-for="c in CHANNELS" :key="c.value" :value="c.value">{{ c.label }}</option>
        </select>
      </label>

      <label class="field grow">
        <span>省份 <em>必选</em></span>
        <ProvincePicker v-model="feed.filters.province" :provinces="provinces" allow-all />
      </label>

      <label class="field">
        <span>时间</span>
        <select v-model="feed.filters.range">
          <option v-for="r in RANGE_PRESETS" :key="r.key" :value="r.key">{{ r.label }}</option>
        </select>
      </label>

      <label class="field">
        <span>关键词</span>
        <input v-model="feed.filters.keyword" placeholder="标题或正文" />
      </label>

      <button v-if="hasProvince" class="ghost" @click="clearProvince">清空省份</button>
    </section>

    <main class="layout">
      <!-- 查询错误横幅：放在正文区而不是顶栏，因为它随这次筛选生灭 -->
      <p v-if="errorInfo" class="banner" :class="errorInfo.tone">
        <strong>{{ errorInfo.title }}</strong>
        <span>{{ errorInfo.hint }}</span>
        <button v-if="errorInfo.action === 'token'" class="banner-act" @click="showSettings = true">
          填写 API Key
        </button>
        <button v-else-if="errorInfo.action === 'retry'" class="banner-act" @click="retry">
          重试
        </button>
      </p>

      <div v-if="!hasProvince" class="pick-hint">
        <p class="pick-title">请选择省份后查看数据</p>
        <p class="muted">
          已覆盖 {{ provinces.length }} 个省级行政区、{{ totalSites }} 个站点、
          {{ totalArticles }} 篇文章。选定省份后展示该省的站点与文章；
          想看全部 94 个来源可输入「全国」。
        </p>
      </div>

      <template v-else>
        <div class="summary">
          <span class="chip">{{ provinceName }}</span>
          <span class="muted">
            {{ sites.length }} 个站点 ·
            {{ provinceCoverage ? provinceCoverage.articleCount : feed.total.value }} 篇
          </span>
          <span v-if="activeSource" class="chip active">
            仅看：{{ activeSource.name }}
            <button class="chip-x" title="取消站点筛选" @click="clearSource">×</button>
          </span>
        </div>

        <!-- 省级是历史快照，且在混显时也提示：琥珀色，属「数据边界」不是「故障」 -->
        <p
          v-if="freshness.stale && feed.filters.channel !== 'power'"
          class="banner notice"
        >
          {{ freshness.text }}
        </p>

        <SiteTable
          :sites="sites"
          :loading="sitesLoading"
          :scope-label="provinceName"
          @pick="pickSite"
        />

        <NewsList
          :items="feed.items.value"
          :loading="feed.loading.value"
          :loading-more="feed.loadingMore.value"
          :has-more="feed.hasMore.value"
          :total="feed.total.value"
          :title="channelLabel"
          :error="feed.error.value"
          :truncated="feed.truncated.value"
          :pages="feed.pages.value"
          @retry="retry"
          :empty-hint="emptyHint"
          @open="(a) => (selected = a)"
          @load-more="feed.loadMore()"
        />
      </template>
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
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  padding: 9px 14px;
}

.brand { display: flex; align-items: baseline; gap: 8px; }
.brand strong { font-size: 16px; }
.status { display: flex; gap: 8px; align-items: center; flex: 1; flex-wrap: wrap; }
.actions { display: flex; gap: 6px; }

.settings {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  padding: 10px 14px;
}

.settings label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; flex: 1; }
.settings input { padding: 6px 10px; border: 1px solid var(--c-border); border-radius: var(--radius-md); }

.toolbar {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  padding: 10px 14px;
}

.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--c-muted, #666); }
.field.grow { flex: 1; min-width: 240px; }
.field em { font-style: normal; color: #a32d2d; }
.field select,
.field input {
  padding: 6px 10px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-md);
  background: var(--c-surface);
  font-size: 13px;
  color: inherit;
}

.layout { display: flex; flex-direction: column; gap: 10px; }

.pick-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 320px;
  background: var(--c-surface);
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-lg);
  text-align: center;
  padding: 32px;
}

.pick-title { font-size: 15px; font-weight: 500; margin: 0; }

.summary { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 13px; }

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border: 1px solid var(--c-border);
  border-radius: 999px;
  background: var(--c-surface);
  font-size: 12px;
}

.chip.active { border-color: #b5d4f4; background: #e6f1fb; color: #185fa5; }
.chip-x { border: none; background: none; cursor: pointer; font-size: 13px; line-height: 1; color: inherit; }

.tag { padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--c-border); }
.tag.ok { color: #27500a; background: #eaf3de; border-color: #c0dd97; }
.tag.danger { color: #791f1f; background: #fcebeb; border-color: #f7c1c1; }

.banner {
  margin: 0;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.banner strong { font-weight: 600; }
.banner.error { background: #fcebeb; color: #791f1f; border: 1px solid #f7c1c1; }
/* 琥珀色 = 数据边界（省级快照），刻意不用红色：那会让人以为中台挂了 */
.banner.notice { background: var(--c-accent-soft); color: #8a4b12; border: 1px solid #fed7aa; }
.banner-act {
  margin-left: auto;
  padding: 2px 10px;
  border: 1px solid currentColor;
  border-radius: 999px;
  background: none;
  color: inherit;
  font-size: 12px;
  cursor: pointer;
}

.ghost { border: 1px solid var(--c-border); background: none; }
</style>
