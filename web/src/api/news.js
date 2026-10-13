/**
 * 新闻数据接口（对接中台 ``/api/v1/articles`` 系列）。
 *
 * 这一层只负责「把中台的字段名与前端用到的字段名对齐」，不加业务逻辑。
 * 返回值统一为 camelCase，模板里就不用到处写 ``item.published_at_cst``。
 */
import { apiGet } from './client.js'

/**
 * 中台文章列表项 → 前端文章对象。
 *
 * ``excerpt`` 是正文前 200 字（中台列表接口刻意不返回全文，避免拖走大字段）。
 * 详情页会再拉一次 ``/articles/{id}`` 拿全文。
 */
function toArticle(raw) {
  return {
    id: raw.id,
    title: raw.title || '（无标题）',
    excerpt: raw.excerpt || '',
    sourceName: raw.source_name || raw.source_name_full || '未知来源',
    adapterName: raw.adapter_name || '',
    provinceCode: raw.province_code || null,
    provinceName: raw.province_name || null,
    category: raw.category || '',
    publishedAt: raw.published_at_cst || null,
    timePrecision: raw.time_precision || 'unknown',
    url: raw.url || '',
    qualityFlags: raw.quality_flags || '[]',
  }
}

/**
 * 拉取新闻列表。
 *
 * 时间参数与中台一致：``range``（today/week/month/custom）与 ``since``/``until``
 * **互斥**，同时传中台会返回 400 —— 这里不做静默取舍，让错误暴露出来。
 */
export async function fetchArticles(params = {}, options = {}) {
  const data = await apiGet('/articles', {
    params: {
      page: params.page ?? 1,
      page_size: params.pageSize ?? 20,
      province: params.province || undefined,
      category: params.category || undefined,
      keyword: params.keyword || undefined,
      source: params.source || undefined,
      range: params.range || undefined,
      since: params.since || undefined,
      until: params.until || undefined,
      exclude_duplicates: params.excludeDuplicates ? 'true' : undefined,
    },
    ...options,
  })
  return {
    total: data?.total ?? 0,
    page: data?.page ?? 1,
    pageSize: data?.page_size ?? 20,
    pages: data?.pages ?? 0,
    items: (data?.items ?? []).map(toArticle),
  }
}

/** 拉取单篇详情（含正文全文）。 */
export async function fetchArticleDetail(id, options = {}) {
  const data = await apiGet(`/articles/${id}`, options)
  const body = data?.body_text || ''
  return {
    ...toArticle(data || {}),
    /**
     * 正文配图。**必须显式透传** —— ``toArticle()`` 是白名单映射，
     * 只挑它认识的字段，不加这行的话 ``images`` 会被整段丢掉，
     * 表现为「接口有 12 张图、前端一张都不显示」。
     */
    images: data?.images || [],
    /**
     * 详情接口**不返回** ``excerpt`` 字段（那是列表接口专有的截断字段）。
     * 不补这一行的话，展开 ``{...列表项, ...详情}`` 时会把列表带过来的摘要
     * 覆盖成空字符串，后续 AI 摘要的句子池就只剩标题 —— 表现为「要点就是标题本身」，
     * 且 caveat 会打印「0 字摘要片段」。这里用正文前 200 字补齐，与列表口径一致。
     */
    excerpt: body.slice(0, 200),
    bodyText: body,
  }
}

/** 命中条数（筛选项变更时先拿总数做提示，不必拉整页）。 */
export async function fetchArticleCount(params = {}, options = {}) {
  const data = await apiGet('/articles/count', {
    params: {
      province: params.province || undefined,
      category: params.category || undefined,
      keyword: params.keyword || undefined,
      range: params.range || undefined,
      since: params.since || undefined,
      until: params.until || undefined,
    },
    ...options,
  })
  return data?.total ?? 0
}

/** 行政区清单（用于地区维度筛选与省份名回填）。 */
export async function fetchProvinces(options = {}) {
  const data = await apiGet('/provinces', options)
  return (data?.items ?? []).map((p) => ({
    code: p.code,
    name: p.name,
    shortName: p.short_name || p.name,
    region: p.region || '',
    isSpecial: Boolean(p.is_special),
  }))
}

/** 来源清单（用于「按站点筛选」与来源健康度展示）。 */
export async function fetchSources(params = {}, options = {}) {
  const data = await apiGet('/sources', {
    params: { page: 1, page_size: params.pageSize ?? 100, category: params.category || undefined },
    ...options,
  })
  return (data?.items ?? []).map((s) => ({
    id: s.id,
    adapterName: s.adapter_name,
    name: s.name,
    provinceCode: s.province_code,
    provinceName: s.province_name,
    category: s.category,
    enabled: Boolean(s.enabled),
    status: s.status,
  }))
}

/** 中台健康状态（顶栏用来显示「中台可达 / 降级」）。 */
export async function fetchHealth(options = {}) {
  return apiGet('/health', options)
}
