/**
 * 新闻流状态：筛选条件 + 分页列表 + 加载/错误状态。
 *
 * 设计取舍
 * --------
 * - **不使用全局状态库**。这个应用只有一个数据源、一份列表，用模块级 ref 足够；
 *   引 Pinia 只会多一层样板。
 * - **筛选变更用竞态令牌（requestId）兜底**。用户快速切换筛选时多个请求会并发，
 *   先发后到的响应可能覆盖后发先到的 —— 这是列表页最常见的「筛选闪回」缺陷。
 *   每次请求带一个自增号，回来时对不上就丢弃。
 *
 * @module composables/useNewsFeed
 */
import { computed, reactive, ref } from 'vue'
import { fetchArticles } from '../api/news.js'
import { toQueryParams } from '../utils/topics.js'

const PAGE_SIZE = 20

export function useNewsFeed() {
  const items = ref([])
  const total = ref(0)
  const page = ref(1)
  const pages = ref(0)
  const loading = ref(false)
  const loadingMore = ref(false)
  const error = ref('')
  const lastLoadedAt = ref(0)

  /** 当前筛选条件。字段与 UI 控件一一对应，便于双向绑定。 */
  const filters = reactive({
    // 数据域：all / power / province。默认 all —— 省级库未接入时后端会
    // 自动降级为 power，并在响应的 channels 字段里如实标注。
    channel: 'all',
    province: '',
    source: '',
    region: '',
    topic: '',
    category: '',
    keyword: '',
    range: 'week',
    since: '',
    until: '',
  })

  let token = 0

  const activeFilterCount = computed(
    () =>
      [filters.province, filters.region, filters.topic, filters.category, filters.keyword]
        .filter(Boolean).length + (filters.range !== 'week' ? 1 : 0),
  )

  const hasMore = computed(() => page.value < pages.value)

  /**
   * 拉第一页。
   *
   * 竞态处理见文件头：``token`` 是自增的请求号，回来时对不上就丢掉结果。
   * @param {string} reason 触发来源（首次 / 定时 / 手动），仅用于 UI 提示。
   */
  async function reload(reason = 'manual') {
    const my = ++token
    loading.value = true
    error.value = ''
    try {
      const params = { ...toQueryParams(filters), page: 1, pageSize: PAGE_SIZE }
      const data = await fetchArticles(params)
      if (my !== token) return { stale: true }
      items.value = data.items
      total.value = data.total
      page.value = data.page
      pages.value = data.pages
      lastLoadedAt.value = Date.now()
      return { ok: true, reason, count: data.items.length, total: data.total }
    } catch (err) {
      if (my !== token) return { stale: true }
      error.value = err?.message || String(err)
      throw err
    } finally {
      if (my === token) loading.value = false
    }
  }

  /** 追加下一页。 */
  async function loadMore() {
    if (!hasMore.value || loadingMore.value) return
    const my = token
    loadingMore.value = true
    try {
      const data = await fetchArticles({
        ...toQueryParams(filters),
        page: page.value + 1,
        pageSize: PAGE_SIZE,
      })
      if (my !== token) return
      // 跨页去重：中台按发布时间倒序，新稿涌入时下一页可能与本页重叠
      const seen = new Set(items.value.map((a) => a.id))
      items.value = [...items.value, ...data.items.filter((a) => !seen.has(a.id))]
      page.value = data.page
      pages.value = data.pages
    } catch (err) {
      error.value = err?.message || String(err)
    } finally {
      loadingMore.value = false
    }
  }

  /** 重置全部筛选。 */
  function reset() {
    Object.assign(filters, {
      channel: 'all',
      province: '', source: '', region: '', topic: '', category: '',
      keyword: '', range: 'week', since: '', until: '',
    })
  }

  /**
   * 清空结果（不清筛选条件）。
   *
   * 「未选省份不展示数据」需要它：用户把省份清空时，列表必须立刻变空，
   * 而不是继续显示上一个省的文章——那会让人以为筛选没生效。
   */
  function clear() {
    token += 1            // 让在途请求的响应失效
    items.value = []
    total.value = 0
    page.value = 1
    pages.value = 0
    error.value = ''
    loading.value = false
    loadingMore.value = false
  }

  return {
    items, total, page, pages, loading, loadingMore, error, lastLoadedAt,
    filters, activeFilterCount, hasMore,
    reload, loadMore, reset, clear,
    pageSize: PAGE_SIZE,
  }
}
