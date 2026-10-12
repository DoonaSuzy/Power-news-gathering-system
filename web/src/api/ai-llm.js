/**
 * 大模型 Provider（**待联调**）。
 *
 * 这是需求 2「接入 AI 大模型总结能力」的**标准对接位**。设计目标：
 * 接口地址、认证参数、模型名全部来自环境变量，对方一旦提供，
 * **只需填 ``.env.local`` 三个变量即可上线，不改任何代码**。
 *
 * @example 联调时只需填这三行
 * ```bash
 * # web/.env.local
  VITE_AI_PROVIDER=llm
 * VITE_LLM_API_URL=https://openrouter.ai/api/v1/chat/completions    # 不要带 /chat/completions
 * VITE_LLM_API_KEY=sk-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
 * VITE_LLM_MODEL=apodex/apodex-1.1-mini:free
 * ```
 *
 * ## 未配置时的行为（刻意设计）
 *
 * ``available()`` 返回 ``false``，于是 ``auto`` 模式下不会选中它；
 * 若用户显式设 ``VITE_AI_PROVIDER=llm``，服务层会**明确报错**说明缺哪一项，
 * 而不是静默降级 —— 静默降级会让「明明配了却走本地规则」变成难以排查的谜。
 *
 * ## 协议
 *
 * OpenAI 兼容的 ``POST {base}/chat/completions``。这是当前事实标准，
 * 通义 / DeepSeek / 智谱 / 自建 vLLM / Ollama 都兼容它。若对方是私有协议，
 * **只需替换本文件**，其余代码不动。
 *
 * @module api/ai-llm
 */

/** ``notices.js`` 是叶子模块，静态引入不会形成循环依赖（不要用动态 import）。 */
import { toNotices } from '../utils/notices.js'

/** 配置键名集中在这里，避免散落。 */
export const LLM_ENV = {
  url: 'VITE_LLM_API_URL',
  key: 'VITE_LLM_API_KEY',
  model: 'VITE_LLM_MODEL',
  timeout: 'VITE_LLM_TIMEOUT_MS',
  maxTokens: 'VITE_LLM_MAX_TOKENS',
}

const DEFAULT_TIMEOUT = 60_000
/**
 * 默认输出上限 8000。
 *
 * 不能省：实测 OpenRouter 上的推理型模型（如 ``apodex-1.1-mini:free``）
 * 一次总结消耗 ``completion_tokens 3987``，其中 **``reasoning_tokens`` 3641** ——
 * 思考过程也计入输出长度。不显式给上限时，文章一多或要点一长就会被截断，
 * 表现是「返回的 JSON 不完整、解析失败」，而报错完全看不出是 token 不够。
 * 给大一些无害：按实际用量计费，不会因为上限大就多花钱。
 */
const DEFAULT_MAX_TOKENS = 8000

/**
 * 期望模型返回的字段，**按能力分别声明**。
 *
 * ⚠️ 曾经是一份全局的 ``['headline', 'key_points']``：``explain`` 返回的是
 * ``paragraphs/entities/terms``、``tag`` 返回的是 ``dimensions``，都被判成
 * 「缺少必需字段」而降级到本地规则 —— 表现是「总结走了大模型，解析和打标却
 * 偷偷走本地」，且 caveats 里只有一句缺字段，看不出是 schema 写错。
 */
const REQUIRED_BY_CAPABILITY = {
  summarize: ['headline', 'key_points'],
  explain: [],
  tag: [],
}

const SUMMARY_PROMPT = `你是电力行业新闻分析师。给你一批电力新闻，请提炼核心要点与亮点。
只依据给定材料，不得引入材料外的信息。合并讲同一件事的多篇稿件。
每条要点是一句完整、可独立阅读的中文陈述句（≤60 字），具体到主体与动作。
亮点挑选有实料的条目：含量化数据、有明确进展（投产/开工/并网/中标/首次）或政策口径变化。
article_ids 只能使用材料中出现过的编号。

只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏：
{"headline":"一句话总览","key_points":[{"text":"要点","article_ids":[1,2]}],
 "highlights":[{"text":"亮点","article_ids":[3]}]}`

const EXPLAIN_PROMPT = `你在为一篇电力新闻做逐段解读。请输出 JSON：
{"paragraphs":[{"role":"导语|数据|表态|展望|正文","text":"该段原文或其凝练"}],
 "entities":[{"text":"实体名","type":"机构|技术|地区|政策"}],
 "terms":[{"term":"术语","gloss":"一句话解释"}]}
只依据给定材料，不引入外部信息。`

const TAG_PROMPT = `请为这批电力新闻打多维度标签。请输出 JSON：
{"dimensions":{"主题":["词"],"主体":["词"],"属性":["词"]}}
主题指业务领域（储能/特高压/光伏…），主体指机构（国家电网/南方电网…），
属性指报道类型（政策/投资/技术/保供/市场/安全）。词必须是材料中出现过的原文片段。`


export class RemoteLlmProvider {
  constructor() {
    this.name = '大模型'
    this.capabilities = new Set(['summarize', 'explain', 'tag', 'notices'])
    this.provider = this.name
  }

  /** 从环境变量取配置。每次读而不是缓存：联调时改 .env 后刷新即生效。 */
  get cfg() {
    const env = import.meta.env
    return {
      url: String(env[LLM_ENV.url] || '').trim(),
      key: String(env[LLM_ENV.key] || '').trim(),
      model: String(env[LLM_ENV.model] || '').trim(),
      timeout: Number(env[LLM_ENV.timeout]) || DEFAULT_TIMEOUT,
      maxTokens: Number(env[LLM_ENV.maxTokens]) || DEFAULT_MAX_TOKENS,
    }
  }

  /** 缺了哪几项配置。给用户看的，所以列的是变量名而不是内部字段。 */
  get missing() {
    const { url, key, model } = this.cfg
    const out = []
    if (!url) out.push(LLM_ENV.url)
    if (!key) out.push(LLM_ENV.key)
    if (!model) out.push(LLM_ENV.model)
    return out
  }

  async available() {
    return this.missing.length === 0
  }

  /** 未配置时抛一个能说明缺什么的错，而不是泛泛的「不可用」。 */
  _requireConfigured() {
    const missing = this.missing
    if (missing.length) {
      throw new Error(`大模型未配置：缺少 ${missing.join(' / ')}（见 web/.env.example）`)
    }
  }

  /** 发一次 chat 请求，返回解析后的 JSON 对象。 */
  async _chat(system, user, capability = 'summarize') {
    this._requireConfigured()
    const { url, key, model, timeout, maxTokens } = this.cfg
    // 用户很可能直接粘贴带 /chat/completions 的完整地址（OpenRouter 文档里就是这样）。
    // 不剥掉会拼成 .../chat/completions/chat/completions → 404，且报错信息看不出原因。
    const base = url.replace(/\/+$/, '').replace(/\/chat\/completions$/i, '')
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeout)
    let resp
    try {
      resp = await fetch(`${base}/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${key}`,
        },
        body: JSON.stringify({
          model,
          messages: [
            { role: 'system', content: system },
            { role: 'user', content: user },
          ],
          temperature: 0.2,
          max_tokens: maxTokens,
          response_format: { type: 'json_object' },
        }),
        signal: controller.signal,
      })
    } catch (err) {
      if (controller.signal.aborted) throw new Error(`大模型调用超时（${timeout}ms）`)
      throw new Error(`大模型网络错误：${err.message}`)
    } finally {
      clearTimeout(timer)
    }

    if (!resp.ok) {
      throw new Error(`大模型返回 HTTP ${resp.status}：${(await resp.text()).slice(0, 200)}`)
    }
    const body = await resp.json().catch(() => null)
    const content = body?.choices?.[0]?.message?.content
    if (typeof content !== 'string' || !content.trim()) {
      throw new Error('大模型响应缺少 choices[0].message.content')
    }
    const parsed = parseLenient(content)
    const missing = (REQUIRED_BY_CAPABILITY[capability] || []).filter((k) => !(k in parsed))
    if (missing.length) throw new Error(`大模型返回的 JSON 缺少字段 ${missing.join(' / ')}`)
    return parsed
  }

  _context(ctx) {
    return {
      articles: ctx?.articles || (ctx?.article ? [ctx.article] : []),
      windowLabel: ctx?.window?.label || '指定周期',
    }
  }

  async summarize(ctx = {}) {
    const { articles, windowLabel } = this._context(ctx)
    const known = new Map(articles.map((a) => [a.id, a]))
    const raw = await this._chat(SUMMARY_PROMPT, buildUserPrompt(articles, windowLabel))
    return {
      headline: String(raw.headline || '').trim(),
      keyPoints: cleanPoints(raw.key_points, known, ctx.maxPoints ?? 6),
      highlights: cleanPoints(raw.highlights, known, ctx.maxHighlights ?? 3),
      keywords: [],
      method: 'llm',
      provider: this.name,
      caveats: [],
      generatedAt: new Date().toISOString(),
    }
  }

  async explain(ctx = {}) {
    const article = ctx.article
    if (!article) throw new Error('explain 需要 ctx.article')
    const raw = await this._chat(
      EXPLAIN_PROMPT,
      `标题：${article.title}\n来源：${article.sourceName || '未知'}\n`
        + `正文：${article.bodyText || article.excerpt || ''}`,
      'explain',
    )
    return {
      paragraphs: Array.isArray(raw.paragraphs) ? raw.paragraphs.slice(0, 12) : [],
      entities: Array.isArray(raw.entities) ? raw.entities.slice(0, 20) : [],
      terms: Array.isArray(raw.terms) ? raw.terms.slice(0, 10) : [],
      method: 'llm',
      provider: this.name,
      caveats: [],
      generatedAt: new Date().toISOString(),
    }
  }

  async tag(ctx = {}) {
    const { articles } = this._context(ctx)
    const raw = await this._chat(TAG_PROMPT, buildUserPrompt(articles, '打标'), 'tag')
    const dimensions = {}
    if (raw.dimensions && typeof raw.dimensions === 'object') {
      for (const [dim, words] of Object.entries(raw.dimensions)) {
        if (Array.isArray(words)) {
          dimensions[dim] = words.map(String).filter(Boolean).slice(0, 12)
        }
      }
    }
    return {
      dimensions,
      labels: articles.map((a) => ({ articleId: a.id, dimensions: {}, primary: '' })),
      method: 'llm',
      provider: this.name,
    }
  }

  async notices(ctx = {}) {
    const summary = await this.summarize(ctx)
    return { notices: toNotices(summary, this.name), summary }
  }
}

// ---------------------------------------------------------------- 工具

/** 拼给模型的 user 消息。带长度截断与截断提示。 */
function buildUserPrompt(articles, windowLabel) {
  const header = `时间窗口：${windowLabel}\n材料共 ${articles.length} 篇电力新闻。\n`
  const parts = []
  let used = header.length
  for (const a of articles) {
    const block = `\n[${a.id}] ${a.title}\n来源：${a.sourceName || '未知'}\n`
      + `${(a.bodyText || a.excerpt || '').slice(0, 400)}\n`
    if (used + block.length > 24000) break
    parts.push(block)
    used += block.length
  }
  const tail = parts.length < articles.length
    ? `\n\n（为控制长度仅提供前 ${parts.length} 篇，请只针对已提供材料总结。）`
    : ''
  return header + parts.join('') + tail
}

/** 清洗模型给的要点数组，**过滤编造的 article_ids**。 */
function cleanPoints(raw, known, cap) {
  if (!Array.isArray(raw)) return []
  const out = []
  // ⚠️ 必须是 slice(0, cap) 而不是 slice(cap)。
  // 后者是「从第 cap 个开始取到末尾」，对 4 条要点、cap=6 会得到**空数组** ——
  // 表现为「headline 有、要点全没」，而模型明明返回了 4 条。这类错误
  // 在只有 1~2 条要点时也会静默出错，很难从现象反推。
  for (const item of raw.slice(0, cap)) {
    if (!item || typeof item !== 'object') continue
    const text = String(item.text || '').trim()
    if (!text) continue
    const ids = Array.isArray(item.article_ids)
      ? [...new Set(item.article_ids.map((v) => Number(v)).filter((v) => known.has(v)))]
      : []
    out.push({
      text,
      articleIds: ids,
      sources: ids.map((i) => known.get(i).sourceName).filter(Boolean),
      reasons: [],
    })
  }
  return out
}

/**
 * 尽最大努力把模型输出解析成 JSON。
 * 依次尝试：整体解析 → 剥 Markdown 围栏 → 抠第一段平衡花括号。
 */
function parseLenient(raw) {
  const text = String(raw || '').trim()
  try {
    const v = JSON.parse(text)
    if (v && typeof v === 'object') return v
  } catch { /* 继续尝试 */ }

  for (const block of text.split('```')) {
    const b = block.trim().replace(/^json/i, '').trim()
    try {
      const v = JSON.parse(b)
      if (v && typeof v === 'object') return v
    } catch { /* 继续 */ }
  }

  const start = text.indexOf('{')
  if (start >= 0) {
    let depth = 0
    let inStr = false
    let esc = false
    for (let i = start; i < text.length; i += 1) {
      const ch = text[i]
      if (inStr) {
        if (esc) esc = false
        else if (ch === '\\') esc = true
        else if (ch === '"') inStr = false
        continue
      }
      if (ch === '"') inStr = true
      else if (ch === '{') depth += 1
      else if (ch === '}') {
        depth -= 1
        if (depth === 0) {
          try {
            const v = JSON.parse(text.slice(start, i + 1))
            if (v && typeof v === 'object') return v
          } catch {
            break
          }
        }
      }
    }
  }
  throw new Error('大模型返回内容不是可解析的 JSON')
}
