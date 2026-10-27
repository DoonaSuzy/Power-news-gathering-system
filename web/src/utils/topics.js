/**
 * 筛选维度预设：地区 + 主题。
 *
 * 主题维度落到中台的 ``keyword`` 参数（标题/正文模糊匹配），而不是靠前端过滤 ——
 * 前端只拿到当前页 20 条，本地过滤出来的「主题」其实是「这一页里的主题」，
 * 与分页一结合就会得出「储能只有 3 条」这种错误结论。
 *
 * 新增主题只需要在这张表里加一行，UI 自动出现（组件遍历它渲染）。
 */

/** 8 大区。与中台 ``newsagg/weather/models.py`` 的大区口径一致。 */
export const REGION_PRESETS = [
  '华北', '东北', '华东', '华中', '华南', '西南', '西北', '港澳台',
]

/**
 * 主题预设。
 *
 * ``keyword`` 是发给中台的搜索词。选词原则：**用领域内的稳定名词**，
 * 不用「太阳能」这类同义词 —— 中台是 LIKE 匹配，同义词会各命中一批，
 * 反而让「光伏」这个主题下的条数看起来偏少。
 */
export const TOPIC_PRESETS = [
  { key: 'storage', label: '储能', keyword: '储能' },
  { key: 'uhv', label: '特高压', keyword: '特高压' },
  { key: 'pv', label: '光伏', keyword: '光伏' },
  { key: 'wind', label: '风电', keyword: '风电' },
  { key: 'hydro', label: '水电', keyword: '水电' },
  { key: 'nuclear', label: '核电', keyword: '核电' },
  { key: 'market', label: '电力市场', keyword: '电力市场' },
  { key: 'grid', label: '电网投资', keyword: '电网' },
  { key: 'hydrogen', label: '氢能', keyword: '氢能' },
  { key: 'vpp', label: '虚拟电厂', keyword: '虚拟电厂' },
  { key: 'carbon', label: '碳市场', keyword: '碳交易' },
  { key: 'security', label: '能源保供', keyword: '保供' },
]

/** 来源类别（中台 ``sources.category``）。 */
export const CATEGORY_PRESETS = [
  { key: 'sgcc', label: '国网系' },
  { key: 'csg', label: '南网系' },
  { key: 'gov', label: '政府能源主管' },
  { key: 'industry', label: '行业媒体' },
  { key: 'hmt', label: '港澳台' },
  { key: 'independent', label: '独立来源' },
]

/** 时间档位。与中台 ``timefilter.RANGE_LABELS`` 对齐。 */
export const RANGE_PRESETS = [
  { key: 'today', label: '今日', days: 1 },
  { key: 'week', label: '近 1 周', days: 7 },
  { key: 'month', label: '近 1 个月', days: 30 },
  { key: 'all', label: '不限时间', days: null },
]

/** 主题 key → 预设对象。 */
export function findTopic(key) {
  return TOPIC_PRESETS.find((t) => t.key === key) || null
}

/**
 * 把 UI 选中的维度整理成中台参数。
 *
 * **时间参数只发一种**：``range`` 与 ``since``/``until`` 在中台是互斥的，
 * 同时发会 400。``all`` 档表示不加时间过滤，此时两者都不发。
 *
 * ``region`` 不需要前端展开成省代码列表：中台的 ``province`` 参数本身就接受
 * 「代码 / 全称 / 简称 / 大区名」（见 ``deps.get_article_query`` 的文档），
 * 传「华南」它会解析成该大区下全部省份。前端自己映射反而会漏掉大区的定义。
 */
export function toQueryParams(filters) {
  const topic = findTopic(filters.topic)
  const params = {
    province: filters.province || filters.region || undefined,
    category: filters.category || undefined,
    keyword: topic?.keyword || filters.keyword || undefined,
    channel: filters.channel || undefined,
    source: filters.source || undefined,
  }
  if (filters.range && filters.range !== 'all') {
    params.range = filters.range
  } else if (filters.since || filters.until) {
    params.since = filters.since || undefined
    params.until = filters.until || undefined
  }
  return params
}
