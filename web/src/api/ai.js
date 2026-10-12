/**
 * AI 能力标准接口层。
 *
 * 这是整个前端**唯一**允许出现「AI 从哪来」这一知识的地方。组件只调
 * ``ai.summarize()`` / ``ai.tag()``，不知道背后是中台、某云厂商还是本地规则。
 * 后续换 AI 服务时，只需要新增一个 Provider 文件并在 :data:`PROVIDERS` 注册，
 * **不改任何组件、composable、模板**。
 *
 * ## 统一契约
 *
 * 所有 Provider 都实现同一组方法，返回值形状一致：
 *
 * | 方法 | 用途 | 返回 |
 * |---|---|---|
 * | ``summarize(ctx)`` | 要点总结（需求 3.3） | :ref:`AiSummary` |
 * | ``explain(ctx)`` | 智能解析（需求 3.2） | :ref:`AiExplain` |
 * | ``tag(ctx)`` | 多维度标签化（需求 4） | :ref:`AiTags` |
 * | ``notices(ctx)`` | 公告栏数据（需求 2） | ``AiNotice[]`` |
 * | ``available()`` | 能力探活 | ``boolean`` |
 *
 * ``ctx`` 统一为 ``{ articles, article, window, topic }``：
 * ``articles`` 是当批新闻（数组），``article`` 是单篇（解析/标签用），
 * ``window`` 是当前时间窗口 ``{range, label, start, end}``。
 *
 * ## 为什么要有 LocalAiProvider
 *
 * 「AI 接口」这件事在实践中会以四种方式不可用：没配 Key、后端没起、额度用尽、
 * 上游 5xx。如果前端只认 HTTP，那这四种情况下 AI 功能会**整体消失**。
 * 所以默认 ``auto``：优先 HTTP，失败自动降级到本地规则实现，并在结果里用
 * ``method`` 字段如实标出走的是哪条路 —— 不假装 AI 算过了。
 *
 * @module api/ai
 */
import { apiGet } from './client.js'
import { LocalAiProvider } from './ai-local.js'
import { RemoteLlmProvider } from './ai-llm.js'
import { toNotices } from '../utils/notices.js'

/**
 * 统一对外的方法契约。新增 Provider 时照这个清单实现即可。
 * @typedef {object} AiProvider
 * @property {string} name 展示用名称。
 * @property {() => Promise<boolean>} available 是否可用。
 * @property {(ctx: object) => Promise<object>} summarize 要点总结。
 * @property {(ctx: object) => Promise<object>} explain 智能解析。
 * @property {(ctx: object) => Promise<object>} tag 多维度标签。
 * @property {(ctx: object) => Promise<object>} notices 公告栏数据。
 */

/** 走中台 AI 接口的实现。中台的 LLM 配置见 ``docs/新闻智能总结接口.md``。 */
export class HttpAiProvider {
  constructor() {
    this.name = '中台 AI'
    /**
     * 显式声明**中台真正提供**的能力。
     *
     * 中台只有 ``/news/summary``（按时间窗口总结一批新闻），**没有**单篇解析
     * 与单篇打标端点。早先的实现用「该文章所在那一天」当窗口去近似单篇，
     * 结果返回的是**当天全部新闻**的要点 —— 与用户点开的这篇毫无关系，
     * 而且看不出错（形状完全合法）。这种「形状对、内容错」的返回比报错更糟，
     * 所以这里如实声明能力边界，不支持的交给本地实现。
     */
    this.capabilities = new Set(['summarize', 'notices'])
  }

  /**
   * 按**上下文**判断这次调用能不能交给中台。
   *
   * 光有静态 ``capabilities`` 不够：``summarize`` 在「列表/公告」场景（一批文章）
   * 中台能做，但在「详情页单篇」场景做不了 —— 中台只有 ``/news/summary``
   * 这个**窗口级**端点，传一篇的日期当窗口会返回那一天**全部**新闻的要点。
   *
   * 实测过这个现象：详情页标题写着「AI 要点总结」，内容却是「近1周共 307 篇
   * 电力新闻」——形状合法、语义全错，比报错更难发现。
   */
  supports(method, ctx) {
    if (!this.capabilities.has(method)) return false
    if (method === 'summarize') {
      // 单篇（或空）上下文交给本地：本地是就这一篇的文本选句，语义正确
      return (ctx?.articles?.length ?? 0) > 1
    }
    return true
  }

  async available() {
    try {
      // 用最轻的一档探测：只要中台在，summary 必然可用（哪怕走抽取式）
      await apiGet('/news/summary', { params: { range: 'today', max_points: 1, max_highlights: 0 } })
      return true
    } catch {
      return false
    }
  }

  /** @returns {Promise<object>} 中台 ``/news/summary`` 的 data 原样映射到前端契约。 */
  async summarize(ctx) {
    const data = await apiGet('/news/summary', {
      params: {
        range: ctx.window?.range || 'week',
        since: ctx.window?.since,
        until: ctx.window?.until,
        province: ctx.filters?.province,
        category: ctx.filters?.category,
        keyword: ctx.filters?.keyword,
        max_points: ctx.maxPoints ?? 6,
        max_highlights: ctx.maxHighlights ?? 3,
        limit: ctx.limit ?? 40,
      },
    })
    return {
      headline: data?.summary?.headline || '',
      keyPoints: mapPoints(data?.summary?.key_points),
      highlights: mapPoints(data?.summary?.highlights),
      keywords: (data?.summary?.keywords || []).map((k) => ({
        term: k.term, docs: k.docs, weight: k.weight,
      })),
      stats: data?.stats || null,
      window: data?.window || null,
      caveats: data?.caveats || [],
      // 中台的 method 有三种取值，这里原样透出，UI 直接展示
      method: data?.method === 'llm' ? 'llm' : 'extractive',
      provider: this.name,
      generatedAt: new Date().toISOString(),
    }
  }

  /**
   * 中台**未提供**逐段解析端点 → 不实现，交给本地规则。
   *
   * 保留这个方法并显式抛错，而不是删掉它：``capabilities`` 已经声明不支持，
   * 正常情况下不会被调用；万一将来有人绕过 capabilities 直接调，
   * 明确的错误比「返回了一堆不相干要点」更容易定位。
   */
  async explain() {
    throw new Error('中台未提供单篇解析端点')
  }

  /** 同上：中台没有单篇打标端点。 */
  async tag() {
    throw new Error('中台未提供单篇打标端点')
  }

  /** 公告栏 = 中台总结的要点与亮点，转成可滚动的条目。 */
  async notices(ctx) {
    const summary = await this.summarize({ ...ctx, maxPoints: 8, maxHighlights: 5 })
    return { notices: toNotices(summary, '中台 AI'), summary }
  }
}

/**
 * 简单 Provider：读取中台数据，但**不做 AI 总结**，只把统计口径返回。
 * 用于中台在但 LLM 与本地都不可用的极端情况（例如浏览器禁用了 Web Worker）。
 */
export class StatsOnlyProvider {
  constructor() {
    this.name = '仅统计'
    this.capabilities = new Set()
  }

  async available() {
    return true
  }

  async summarize() {
    return emptySummary('stats-only')
  }

  async explain() {
    return { paragraphs: [], entities: [], terms: [], caveats: ['AI 能力不可用'], method: 'none', provider: this.name }
  }

  async tag() {
    return { dimensions: {}, method: 'none', provider: this.name }
  }

  async notices() {
    return { notices: [] }
  }
}

// ---------------------------------------------------------------- 工具

function mapPoints(list) {
  return (list || []).map((p) => ({
    text: p.text || '',
    articleIds: p.article_ids || [],
    sources: p.sources || [],
    reasons: p.reasons || [],
    score: p.score ?? null,
  }))
}

function emptySummary(method) {
  return {
    headline: '', keyPoints: [], highlights: [], keywords: [], stats: null,
    window: null, caveats: [], method, provider: 'none',
    generatedAt: new Date().toISOString(),
  }
}

/**
 * 公告栏条目映射已抽到 ``utils/notices.js``。
 *
 * 原因：本模块要 import ``ai-local.js`` 做降级，而 ``ai-local.js`` 也需要这个
 * 映射；留在这里就构成循环依赖（ESM 能忍，但打包与 tree-shaking 会变脆）。
 * 这里转出一次，保持 ``import { toNotices } from './ai.js'`` 的旧写法仍可用。
 */
export { toNotices, NOTICE_KIND_LABEL } from '../utils/notices.js'

// ---------------------------------------------------------------- 服务出口

/**
 * 可选的 provider 名 → 构造函数。新增 AI 服务在这里加一行即可。
 *
 * ``llm`` 是需求 2 预留的**标准大模型对接位**：对方提供接口地址与认证参数后，
 * 只需在 ``.env.local`` 填 ``VITE_LLM_API_URL`` / ``VITE_LLM_API_KEY`` /
 * ``VITE_LLM_MODEL`` 三个变量并把 ``VITE_AI_PROVIDER`` 设为 ``llm``，即完成联调。
 */
const PROVIDERS = {
  http: () => new HttpAiProvider(),
  local: () => new LocalAiProvider(),
  llm: () => new RemoteLlmProvider(),
}

function resolveChain(mode) {
  const want = (mode || 'auto').toLowerCase()
  if (want === 'http') return [PROVIDERS.http()]
  if (want === 'local') return [PROVIDERS.local()]
  if (want === 'llm') return [PROVIDERS.llm()]
  // auto：**大模型优先**（配了就用，这正是用户配它的目的），
  // 没配则退中台，再退本地规则，最后「仅统计」保底。
  //
  // ⚠️ 曾经漏写上面的 `llm` 分支：`VITE_AI_PROVIDER=llm` 会落到这个 auto 链里，
  // 表现为「明明配了大模型，界面却显示中台 AI」—— 配置看着生效了，实际没走。
  return [
    PROVIDERS.llm(),
    PROVIDERS.http(),
    PROVIDERS.local(),
    new StatsOnlyProvider(),
  ]
}

/**
 * 这次调用能不能交给该 provider。
 *
 * 优先用 provider 自带的 ``supports(method, ctx)``（能按上下文判断），
 * 没有就退回静态 ``capabilities``。两者都没有（第三方实现偷懒）视为支持，
 * 让调用去试 —— 失败会走 catch 降级，不会静默出错。
 */
function isSupported(provider, method, ctx) {
  if (typeof provider.supports === 'function') return provider.supports(method, ctx)
  if (provider.capabilities) return provider.capabilities.has(method)
  return true
}

/**
 * 构造 AI 服务。
 *
 * ``auto`` 模式下会**逐个探测** provider 可用性并把第一个可用的固化下来
 * （探活结果缓存，不在每次调用时重探 —— 那会让每个操作多一次网络往返）。
 */
export function createAiService(mode) {
  const chain = resolveChain(mode)
  let active = null
  let probing = null

  async function pick() {
    if (active) return active
    if (!probing) {
      probing = (async () => {
        for (const candidate of chain) {
          try {
            if (await candidate.available()) return candidate
          } catch {
            // 探测失败就试下一个，不让异常冒到调用方
          }
        }
        return chain[chain.length - 1]
      })().finally(() => { probing = null })
    }
    active = await probing
    return active
  }

  /** 每次调用都记录来源，UI 才能如实显示「这条是谁算的」。 */
  async function call(method, ctx) {
    const provider = await pick()

    // 能力边界：这次调用 provider 做不了（静态不支持，或上下文不匹配）时
    // 不发那次注定语义错误的调用，直接交给本地实现，并说明为什么。
    if (!isSupported(provider, method, ctx)) {
      const local = PROVIDERS.local()
      const result = await local[method](ctx)
      return {
        ...result,
        provider: local.name,
        caveats: [
          ...(result.caveats || []),
          `${provider.name}不适用于本次「${method}」调用（${describeContext(method, ctx)}），已由${local.name}完成`,
        ],
      }
    }

    try {
      const result = await provider[method](ctx)
      return { ...result, provider: result.provider || provider.name }
    } catch (err) {
      // 已选 provider 中途失败（额度用尽、后端重启）：本次退回本地，并**不**固化
      const fallback = PROVIDERS.local()
      const result = await fallback[method](ctx)
      return {
        ...result,
        provider: fallback.name,
        caveats: [...(result.caveats || []), `${provider.name} 调用失败（${err.message}），已本地降级`],
      }
    }
  }

  /** 给用户看的一行上下文说明，避免 caveat 说得含糊。 */
  function describeContext(method, ctx) {
    if (method === 'summarize') {
      const n = ctx?.articles?.length ?? 0
      return n <= 1 ? `输入是单篇（${n} 篇），中台只有窗口级总结端点` : `输入 ${n} 篇`
    }
    return '中台未提供该端点'
  }

  return {
    /** 当前生效的 provider 名（探活后才有值）。 */
    async currentName() {
      return (await pick()).name
    },
    summarize: (ctx = {}) => call('summarize', ctx),
    explain: (ctx = {}) => call('explain', ctx),
    tag: (ctx = {}) => call('tag', ctx),
    notices: (ctx = {}) => call('notices', ctx),
  }
}

/** 应用级单例。模式来自 ``VITE_AI_PROVIDER``，默认 auto。 */
export const ai = createAiService(import.meta.env.VITE_AI_PROVIDER || 'auto')
