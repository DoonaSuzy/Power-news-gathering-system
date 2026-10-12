/**
 * 把 AI 结构化结果转成公告栏条目。
 *
 * 放在独立模块而不是留在 ``api/ai.js``：``ai.js`` 要 import ``ai-local.js``
 * 做降级，而 ``ai-local.js`` 需要 ``toNotices`` —— 放在 ai.js 里会形成循环依赖。
 * 抽成叶子模块后两边都能引，且这个映射规则将来只会在「公告栏要展示什么」
 * 这个需求变动时修改，与其他部分解耦。
 */

/** 公告栏条目类型 → 展示样式。 */
export const NOTICE_KIND_LABEL = {
  highlight: '亮点',
  keypoint: '要点',
  keyword: '焦点',
}

/** 与本地算法的量化数据判定保持同一份正则，避免两条路径口径漂移。 */
const QUANTITY_RE =
  /[±+\-]?\d+(?:\.\d+)?\s*(?:亿|万|千|百)?\s*(?:千瓦时|千瓦|千伏安|千伏|兆瓦|吉瓦|kWh|MWh|GWh|GW|MW|kW|kV|kVA|吨|万吨|%|％|个百分点|亿元|万元|元|台|座|个|项|家|条|公里|千米)/

/**
 * 转公告条目。
 *
 * **置顶规则是显式的**（不是「看起来重要的就置顶」）：
 * 带「含量化数据」原因的亮点置顶 —— 数字最容易被核实，也最该先看到。
 * 其余按 亮点 → 要点 → 焦点 排列，组内保持原顺序（稳定，不因置顶打乱可读性）。
 *
 * ⚠️ 置顶判定**不能只信 ``reasons``**：LLM 路径的要点不带 reasons（那是本地
 * 算法的产物），只信它会让「配了大模型反而没有置顶」—— 功能随 provider
 * 切换而消失。所以这里用文本特征兜底判断，两条路径口径一致。
 */
export function toNotices(summary, providerName, limit = 12) {
  const out = []
  const push = (item) => {
    if (out.length < limit) out.push(item)
  }

  const isQuantified = (p) =>
    (p.reasons || []).some((r) => String(r).startsWith('含量化数据'))
    || QUANTITY_RE.test(p.text || '')

  for (const p of summary?.highlights || []) {
    const quantified = isQuantified(p)
    push({
      id: `hl-${out.length}`,
      kind: 'highlight',
      pinned: quantified,
      pinReason: quantified ? '含量化数据' : '',
      text: p.text,
      sources: p.sources || [],
      articleIds: p.articleIds || [],
      reasons: p.reasons || [],
      provider: providerName,
    })
  }
  for (const p of summary?.keyPoints || []) {
    push({
      id: `kp-${out.length}`,
      kind: 'keypoint',
      pinned: false,
      pinReason: '',
      text: p.text,
      sources: p.sources || [],
      articleIds: p.articleIds || [],
      reasons: p.reasons || [],
      provider: providerName,
    })
  }
  if (summary?.keywords?.length) {
    push({
      id: `kw-${out.length}`,
      kind: 'keyword',
      pinned: false,
      pinReason: '',
      text: `本期焦点：${summary.keywords.slice(0, 8).map((k) => `${k.term}(${k.docs ?? '-'})`).join('、')}`,
      sources: [],
      articleIds: [],
      reasons: [],
      provider: providerName,
    })
  }

  return [...out.filter((n) => n.pinned), ...out.filter((n) => !n.pinned)]
}
