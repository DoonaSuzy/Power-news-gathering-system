/**
 * 本地 AI 降级实现：不依赖任何后端与模型的确定性规则算法。
 *
 * 定位
 * ----
 * 它不是「假 AI」，而是**保证功能不消失的兜底**。中台没起、LLM 没配、
 * 额度用尽、上游 5xx —— 这四种情况下 AI 功能仍照常可用，只是结果里
 * ``method`` 会如实写成 ``local-rule``，UI 会显示「本地规则」而不是「AI」。
 *
 * 与后端 ``extractive.py`` 的关系
 * ------------------------------
 * 思路同构（切句 → 领域词打分 → 去冗余选取），但**不追求结果一致**：
 * 前端只有列表接口给的 200 字 ``excerpt``，没有全文，做 IDF 也没有语料库。
 * 所以这里用「领域词命中 + 量化数据 + 位置」三个可解释的信号打分，
 * 结果一定不如后端，但**永远可用且完全可解释**。
 *
 * @module api/ai-local
 */
import { GLOSSARY, QUANTITY_RE, matchDimensions } from '../utils/lexicon.js'
import { toNotices } from '../utils/notices.js'

const SENT_SPLIT = /(?<=[。！？；!?;])/
const MIN_SENT = 8
const MAX_SENT = 200

/** 切句：按句末标点切，过滤过短碎片。 */
function splitSentences(text) {
  return String(text || '')
    .split(/\r?\n/)
    .flatMap((line) => line.split(SENT_SPLIT))
    .map((s) => s.trim())
    .filter((s) => s.length >= MIN_SENT && s.length <= MAX_SENT)
}

/**
 * 句子打分。三个信号加权，权重是**显式常量**（不是拍脑袋的魔数散落在代码里）：
 * - 领域词命中（每一类维度算一次）；
 * - 量化数据（数字最容易被核实，也最值得作为要点）；
 * - 位置（越靠前的句子越可能是导语/结论）。
 */
const W_DIMENSION = 2.0
const W_QUANTITY = 2.5
const W_POSITION = 1.5

function scoreSentence(sent, index, total) {
  const dims = matchDimensions(sent)
  const dimCount = dims.主题.length + dims.主体.length + dims.属性.length
  const base = dimCount * W_DIMENSION + (QUANTITY_RE.test(sent) ? W_QUANTITY : 0)
  const position = total > 1 ? 1 - index / total : 1
  // 长度归一化：不归一化的话长句因为词多必然占优，选出来全是又臭又长的段落
  return (base + position * W_POSITION) / Math.sqrt(Math.max(1, sent.length / 30))
}

/** 两个句子的字符二元组 Jaccard 相似度（去冗余用，无需分词）。 */
function similarity(a, b) {
  const grams = (s) => {
    const set = new Set()
    for (let i = 0; i < s.length - 1; i += 1) set.add(s.slice(i, i + 2))
    return set
  }
  const ga = grams(a)
  const gb = grams(b)
  if (!ga.size || !gb.size) return 0
  let hit = 0
  for (const g of ga) if (gb.has(g)) hit += 1
  return hit / (ga.size + gb.size - hit)
}

/**
 * 贪心选句：每次取「分数 − 与已选句最大相似度」最高的一句。
 *
 * 不做这一步的话，同一事件的六篇转载会把要点位置全占满 ——
 * 这是抽取式摘要最典型的失败模式。
 */
function pickSentences(candidates, k) {
  const picked = []
  const pool = [...candidates]
  while (pool.length && picked.length < k) {
    let bestIdx = 0
    let bestVal = -Infinity
    for (let i = 0; i < pool.length; i += 1) {
      const redundancy = picked.length
        ? Math.max(...picked.map((p) => similarity(pool[i].text, p.text)))
        : 0
      const value = pool[i].score - redundancy * 3
      if (value > bestVal) {
        bestVal = value
        bestIdx = i
      }
    }
    picked.push(pool.splice(bestIdx, 1)[0])
  }
  return picked
}

function buildHeadline(articles, dims, windowLabel) {
  const total = articles.length
  if (!total) return `${windowLabel || '该窗口'}没有采集到符合条件的电力新闻。`
  const sources = new Set(articles.map((a) => a.sourceName).filter(Boolean)).size
  const focus = Object.entries(dims)
    .flatMap(([, words]) => words)
    .slice(0, 5)
  return focus.length
    ? `${windowLabel || '该窗口'}共 ${total} 篇电力新闻，来自 ${sources} 个站点，焦点集中在：${focus.join('、')}。`
    : `${windowLabel || '该窗口'}共 ${total} 篇电力新闻，来自 ${sources} 个站点。`
}

/** 本地 Provider。契约见 ``api/ai.js`` 的模块说明。 */
export class LocalAiProvider {
  constructor() {
    this.name = '本地规则'
    // 本地规则四项全能：这正是它作为兜底的意义
    this.capabilities = new Set(['summarize', 'explain', 'tag', 'notices'])
  }

  async available() {
    // 纯浏览器计算，永远可用
    return true
  }

  /** @returns {Promise<object>} 与 HttpAiProvider.summarize 同形状。 */
  async summarize(ctx = {}) {
    const articles = ctx.articles || []
    const windowLabel = ctx.window?.label || ''
    const maxPoints = ctx.maxPoints ?? 6
    const maxHighlights = ctx.maxHighlights ?? 3

    const candidates = []
    //: 单篇可用于摘要的文本上限。详情页会给到全文（实测 6000 字 ≈ 120 句），
    //: 句子池线性膨胀会让打分变慢，而要点几乎总在前 3000 字里出现。
    const MAX_TEXT = 3000
    let usedChars = 0
    for (const [ai, article] of articles.entries()) {
      // 优先正文（详情页有），回退列表摘要（列表页只有 200 字）
      const source = String(article.bodyText || article.excerpt || '')
      const text = source.slice(0, MAX_TEXT)
      usedChars = Math.max(usedChars, text.length)

      // 标题单独入池：很多站点的摘要只有一句导语，丢掉标题就没料了
      const title = String(article.title || '').trim()
      if (title.length >= MIN_SENT) {
        candidates.push({
          text: title,
          articleId: article.id,
          sourceName: article.sourceName,
          score: scoreSentence(title, ai, articles.length) * 1.35,
          fromTitle: true,
        })
      }
      for (const [si, sent] of splitSentences(text).entries()) {
        candidates.push({
          text: sent,
          articleId: article.id,
          sourceName: article.sourceName,
          score: scoreSentence(sent, si, 10),
          fromTitle: false,
        })
      }
    }

    if (!candidates.length) {
      return {
        headline: buildHeadline(articles, {}, windowLabel),
        keyPoints: [], highlights: [], keywords: [], stats: null, window: null,
        caveats: ['参与摘要的文章没有可用正文，无法提取要点。'],
        method: 'local-rule', provider: this.name,
        generatedAt: new Date().toISOString(),
      }
    }

    const ranked = [...candidates].sort((a, b) => b.score - a.score)
    const points = pickSentences(ranked, maxPoints).map((p) => ({
      text: p.text,
      articleIds: [p.articleId],
      sources: p.sourceName ? [p.sourceName] : [],
      reasons: reasonsFor(p),
      score: Number(p.score.toFixed(3)),
    }))

    // 亮点单独选：含量化数据的句子，且尽量不重复要点已选的句子
    const usedTexts = new Set(points.map((p) => p.text))
    const highlights = ranked
      .filter((c) => QUANTITY_RE.test(c.text))
      .filter((c) => !usedTexts.has(c.text))
      .slice(0, maxHighlights)
      .map((c) => ({
        text: c.text,
        articleIds: [c.articleId],
        sources: c.sourceName ? [c.sourceName] : [],
        reasons: reasonsFor(c),
        score: Number(c.score.toFixed(3)),
      }))

    const dims = this._aggregateDimensions(articles)
    const keywords = Object.entries(dims)
      .flatMap(([dim, words]) => words.map((w) => ({ term: w, dimension: dim })))
      .slice(0, 12)

    return {
      headline: buildHeadline(articles, dims, windowLabel),
      keyPoints: points,
      highlights,
      keywords: keywords.map((k) => ({ term: k.term, docs: countDocs(articles, k.term), weight: countDocs(articles, k.term) })),
      stats: null,
      window: null,
      caveats: [
        '当前使用浏览器本地规则实现（未接入 AI 服务）：仅基于关键词与量化特征，质量低于模型总结。',
        `本地规则基于 ${usedChars} 字文本（${articles[0]?.bodyText ? '详情页全文' : '列表摘要'}）提取要点，不做语义理解。`,
      ],
      method: 'local-rule',
      provider: this.name,
      generatedAt: new Date().toISOString(),
    }
  }

  /**
   * 智能解析单篇。
   *
   * 输出三类可核验的东西，而不是一段模型味的转述：
   * - ``paragraphs``：逐段角色（导语 / 数据 / 表态 / 展望 / 正文）；
   * - ``entities``：从词表命中的人机构与技术词；
   * - ``terms``：命中的术语给出词典解释。
   */
  async explain(ctx = {}) {
    const article = ctx.article
    if (!article) {
      return {
        paragraphs: [], entities: [], terms: [],
        caveats: ['缺少文章内容，无法解析。'], method: 'local-rule', provider: this.name,
      }
    }
    const text = `${article.title || ''}。${article.bodyText || article.excerpt || ''}`
    const sentences = splitSentences(text)

    const paragraphs = sentences.slice(0, 8).map((s, i) => ({
      role: roleOf(s, i),
      text: s,
    }))

    const dims = matchDimensions(text)
    const entities = [
      ...dims.主体.map((t) => ({ text: t, type: '机构' })),
      ...dims.主题.map((t) => ({ text: t, type: '技术/业务' })),
    ]

    const terms = Object.entries(GLOSSARY)
      .filter(([term]) => text.includes(term))
      .slice(0, 6)
      .map(([term, gloss]) => ({ term, gloss }))

    return {
      paragraphs,
      entities,
      terms,
      caveats: ['本地规则解析：段落角色与实体由关键词判定，不做语义理解。'],
      method: 'local-rule',
      provider: this.name,
      generatedAt: new Date().toISOString(),
    }
  }

  /** 多维度标签化（需求 4）。 */
  async tag(ctx = {}) {
    const articles = ctx.article ? [ctx.article] : ctx.articles || []
    const dimensions = this._aggregateDimensions(articles)
    const labels = articles.map((a) => {
      const text = `${a.title || ''} ${a.excerpt || ''} ${a.bodyText || ''}`
      const dims = matchDimensions(text)
      return {
        articleId: a.id,
        dimensions: dims,
        primary: dims.主题[0] || dims.属性[0] || '其他',
      }
    })
    return {
      dimensions,
      labels,
      method: 'local-rule',
      provider: this.name,
    }
  }

  async notices(ctx = {}) {
    const summary = await this.summarize({ ...ctx, maxPoints: 8, maxHighlights: 5 })
    return { notices: toNotices(summary, this.name), summary }
  }

  /** 汇总一批文章命中的维度词，按覆盖篇数降序。 */
  _aggregateDimensions(articles) {
    const counter = new Map()
    for (const a of articles) {
      const text = `${a.title || ''} ${a.excerpt || ''}`
      const dims = matchDimensions(text)
      for (const [dim, words] of Object.entries(dims)) {
        for (const w of words) {
          const key = `${dim}:${w}`
          const cur = counter.get(key) || { dimension: dim, term: w, docs: 0 }
          cur.docs += 1
          counter.set(key, cur)
        }
      }
    }
    const grouped = {}
    for (const { dimension, term } of [...counter.values()].sort((a, b) => b.docs - a.docs)) {
      grouped[dimension] = grouped[dimension] || []
      if (!grouped[dimension].includes(term)) grouped[dimension].push(term)
    }
    return grouped
  }
}

// ---------------------------------------------------------------- 内部工具

function reasonsFor(candidate) {
  const reasons = []
  if (QUANTITY_RE.test(candidate.text)) reasons.push('含量化数据')
  if (candidate.fromTitle) reasons.push('来自标题')
  return reasons
}

function roleOf(sentence, index) {
  if (index === 0) return '导语'
  if (QUANTITY_RE.test(sentence)) return '数据'
  if (/(表示|指出|强调|要求|提出|认为)/.test(sentence)) return '表态'
  if (/(下一步|未来|将|计划|拟|展望)/.test(sentence)) return '展望'
  return '正文'
}

function countDocs(articles, term) {
  return articles.filter((a) => `${a.title || ''} ${a.excerpt || ''}`.includes(term)).length
}
