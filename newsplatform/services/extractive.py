"""抽取式摘要：不依赖外部模型与分词库的**确定性**要点提取。

为什么要有这个模块
------------------
中台要提供「AI Agent 智能总结」，但 LLM 是**可能不可用**的：
没配 API Key、网络不通、额度用尽、上游 5xx。如果总结接口只走 LLM，
那它在这四种情况下会整体不可用 —— 对「看看今天有什么重点」这种高频只读需求
是不可接受的。

所以设计成两条路：

======== ================================================== ==================
路径     实现                                                何时用
======== ================================================== ==================
``llm``  调大模型做抽象式总结（见 ``core/llm.py``）            配了 Key 且调用成功
``extractive`` 本模块，纯函数打分选句                        ``auto`` 且 LLM 不可用
======== ================================================== ==================

本模块**不引入任何新依赖**（项目只装了 httpx/pandas/numpy，没有 jieba）。
中文按 2-gram / 3-gram 切，可读关键词靠 :data:`DOMAIN_TERMS` 领域词表命中 ——
纯 n-gram 当关键词会输出「家电网」这种读不懂的碎片。

确定性
------
同样的 :class:`Doc` 列表 + 同样的参数 → **逐字节相同**的输出。
算法里没有任何随机数、没有 ``hash()`` 取模、没有字典序依赖之外的遍历顺序，
所以可以直接写断言单测，不需要固定随机种子。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "Doc",
    "KeyPoint",
    "Extraction",
    "DOMAIN_TERMS",
    "split_sentences",
    "tokenize",
    "extract_keywords",
    "starts_with_reference",
    "summarize",
]

# --------------------------------------------------------------------------- 语料


#: 停用词。作用是压掉「的/了/我们」这类在**任何**句子里都高频、因而毫无区分度的词。
#: 只收通用虚词与新闻套话，不收领域词（领域词是信号，见 DOMAIN_TERMS）。
STOPWORDS: frozenset[str] = frozenset({
    "我们", "他们", "你们", "自己", "这个", "那个", "这样", "那样", "什么", "怎么",
    "为了", "因为", "所以", "但是", "而且", "并且", "如果", "虽然", "however",
    "记者", "报道", "日电", "讯", "表示", "指出", "认为", "介绍", "据悉", "日前",
    "进行", "通过", "以及", "同时", "目前", "当前", "已经", "可以", "能够", "需要",
    "相关", "有关", "方面", "情况", "问题", "工作", "重要", "进一步", "不断", "持续",
    "以来", "之后", "之前", "其中", "主要", "包括", "实现", "推进", "加强", "提升",
    "着力", "扎实", "切实", "积极", "全面", "深入", "有效", "充分", "严格", "认真",
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
})

#: 电力领域词表。两个用途：
#: ① 作为**可读关键词**的候选（响应里给用户看的）；
#: ② 给命中领域词的句子加权 —— 一句满是「公司召开会议」的套话，不该压过
#:    一句讲「特高压直流投产」的实料。
#:
#: 分组只为可读性，匹配时不区分来源组。
DOMAIN_TERMS: tuple[str, ...] = (
    # 电网
    "特高压", "输电线路", "变电站", "配电网", "农村电网", "电网投资", "主网架",
    "电力调度", "输配电价", "柔性直流", "直流输电", "交流输电", "gis", "智能电网",
    "配网自动化", "电网侧", "供电可靠性", "线损",
    # 电源
    "火电", "煤电", "水电", "核电", "风电", "光伏", "海上风电", "分布式光伏",
    "生物质", "抽水蓄能", "储能", "新型储能", "电化学储能", "压缩空气", "光热",
    "燃气发电", "气电", "清洁能源", "新能源", "可再生能源", "装机容量", "机组",
    # 市场
    "电力现货", "中长期交易", "绿电", "绿证", "碳交易", "碳市场", "辅助服务",
    "需求响应", "虚拟电厂", "容量电价", "峰谷电价", "分时电价", "电力市场",
    "现货市场", "电力交易", "售电公司", "代理购电", "隔墙售电", "绿电交易",
    # 政策
    "碳达峰", "碳中和", "新型电力系统", "能源保供", "十五五", "十四五", "双碳",
    "电力体制改革", "能源安全", "有序用电", "迎峰度夏", "迎峰度冬",
    # 指标
    "发电量", "用电量", "全社会用电量", "最大负荷", "负荷", "利用小时",
    "弃风", "弃光", "消纳", "外送", "受入", "省间", "跨省",
    # 新业态
    "车网互动", "充换电", "充电桩", "氢能", "绿氢", "综合能源", "能源互联网",
    "人工智能", "数字化", "智慧能源", "微电网", "源网荷储", "电力算力",
)

#: 「亮点」判定用的动作词：出现即说明这条是**进展**而不是表态。
ACHIEVEMENT_TERMS: tuple[str, ...] = (
    "投产", "开工", "并网", "投运", "建成", "贯通", "发电", "中标", "签约",
    "启动", "落地", "获批", "突破", "刷新", "首次", "创新", "发布", "上线",
    "达成", "完成", "超过", "达到", "创下", "新增", "扩建", "核准",
)

#: 数字 + 单位。用来识别「带实料」的句子：「±800千伏」「1.2亿千瓦时」「23.5%」。
#: 注意 ``千伏`` 这类量词要列出，否则「±800千伏特高压」会被当成没有数字。
_QUANTITY = re.compile(
    r"[±+\-]?\d+(?:\.\d+)?\s*"
    r"(?:亿|万|千|百)?\s*"
    r"(?:千瓦时|千瓦|千伏安|千伏|兆瓦|吉瓦|千瓦|"
    r"kWh|MWh|GWh|GW|MW|kW|kV|kVA|"
    r"吨|万吨|亿吨|%|％|个百分点|亿元|万元|元|"
    r"台|座|个|项|家|条|公里|千米|万亩|万亩)"
)

#: 句子切分：中英文句末标点后断句，标点保留在前句。
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")
#: 段落换行也当断句（新闻正文常把导语和正文用换行隔开）。
_LINE_SPLIT = re.compile(r"[\r\n]+")

#: ASCII 词：英文单词（可带连字符/点）、纯数字。
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-_.]{1,}|(?<!\d)\d+(?:\.\d+)?")
#: 连续汉字段。
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")

#: 句子长度上下限（字符）。短于下限的多半是栏目名/时间戳，长于上限的是段落没切开。
MIN_SENT_LEN = 8
MAX_SENT_LEN = 220

#: 补上文后允许的最大长度。补上文是为了让代词有归属，不是为了把段落搬进来，
#: 所以给一个比 ``MAX_SENT_LEN`` 宽但仍有限的上界。
MAX_POINT_CHARS = 320

#: 以指代 / 连接词开头的句子。它们**离开上文就读不懂**：
#: 「该项目锚定沙戈荒大基地调峰电源定位…」—— 不看上一句就不知道「该项目」指谁。
#:
#: 实测（2026-10-12，`?range=week`）9 条产出里有 4 条以这类词开头，
#: 其中「该项目」指中卫电厂、「其中」指可再生能源装机总量，都只在上一句里。
#: 抽取式不改写，所以唯一忠实的修法是**把上文接回来**（见 :func:`_with_prefix`）。
#:
#: ⚠️ **只收指示代词与连接词，不收光杆主体名词**。
#: 「项目自元月首台机组投产后…」这类句子在本文语境下是可读的（文章讲的就是这个
#: 项目），把它一并标记会触发无谓的补上文；而当上一句本身很长（>320 字）时补不进去，
#: 它的「上一句太长」就会变成一条永远修不掉的审计 FAIL —— 误标制造假阳性。
REFERENCE_LEADS = re.compile(
    r"^(?:"
    r"其中|该[一-龥]{0,4}|此[一-龥]{0,2}|这[一-龥]{0,2}|"
    r"上述|前述|据此|由此|因此|所以|同时|此外|另外|而且|并且|但是|然而|"
    r"随后|随即|接着"
    r")"
)

#: 补上文时最多向前回溯几句。两句已能覆盖「上一句本身也是代词开头」的情形，
#: 再多就会把无关内容拖进来。
MAX_CONTEXT_BACK = 2


# --------------------------------------------------------------------------- 数据


@dataclass(frozen=True, slots=True)
class Doc:
    """参与总结的一篇新闻（由仓储层从 ``articles`` 投影而来）。"""

    id: int
    title: str
    body: str
    source_name: str
    published_at: datetime | None = None
    url: str = ""
    province_name: str | None = None

    @property
    def text(self) -> str:
        """标题 + 正文。标题带着核心信息，丢了可惜。"""
        return f"{self.title}。{self.body}" if self.body else self.title


@dataclass(frozen=True, slots=True)
class KeyPoint:
    """一条要点 / 亮点。"""

    #: 要点正文（取自原文句子，不改写 —— 抽取式不生成新文本）
    text: str
    #: 支撑这条要点的文章 id（同义句会被合并，所以可能不止一个）
    article_ids: tuple[int, ...]
    #: 出处站点名
    sources: tuple[str, ...]
    #: 得分，越大越重要。用于解释排序，不参与任何阈值判断
    score: float
    #: 仅 highlights 会填：这条为什么算亮点（如 ``["含量化数据", "含进展词:投产"]``）
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "article_ids": list(self.article_ids),
            "sources": list(self.sources),
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class Extraction:
    """抽取式摘要的结果。"""

    headline: str
    key_points: tuple[KeyPoint, ...]
    highlights: tuple[KeyPoint, ...]
    #: ``(词, 出现篇数, 权重)``，按权重降序
    keywords: tuple[tuple[str, int, float], ...]
    #: 参与统计的文章数
    doc_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "headline": self.headline,
            "key_points": [p.as_dict() for p in self.key_points],
            "highlights": [p.as_dict() for p in self.highlights],
            "keywords": [
                {"term": term, "docs": docs, "weight": round(weight, 4)}
                for term, docs, weight in self.keywords
            ],
            "doc_count": self.doc_count,
        }


# --------------------------------------------------------------------------- 切分


def split_sentences(text: str) -> list[str]:
    """把正文切成句子，过滤过短 / 过长的碎片。

    先按换行切段、再按句末标点切句 —— 新闻正文里「导语\\n正文」很常见，
    不切换行的话导语会和第一段正文粘成一句。
    """
    out: list[str] = []
    for line in _LINE_SPLIT.split(text or ""):
        for chunk in _SENT_SPLIT.split(line):
            sent = chunk.strip()
            if MIN_SENT_LEN <= len(sent) <= MAX_SENT_LEN:
                out.append(sent)
    return out


def tokenize(text: str) -> list[str]:
    """切词：ASCII 词按词切，中文按 2-gram + 3-gram，过滤停用词与纯数字。

    为什么不上 4-gram：中文电力新闻里 2/3 字词已覆盖绝大多数术语
    （特高压、储能、光伏、现货），4-gram 只会把「特高压直流」这类
    本可以命中的领域词切碎成更长的噪声。
    """
    tokens: list[str] = []
    lowered = (text or "").lower()

    for word in _ASCII_TOKEN.findall(lowered):
        if len(word) >= 2 and word not in STOPWORDS:
            tokens.append(word)

    for run in _HAN_RUN.findall(lowered):
        if run in STOPWORDS:
            continue
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                gram = run[i:i + n]
                if gram not in STOPWORDS:
                    tokens.append(gram)
    return tokens


def _sentence_tokens(sent: str) -> set[str]:
    """句子的词集合（去重）。只用于打分与相似度，不关心频次。"""
    return set(tokenize(sent))


def _domain_hits(text: str) -> list[str]:
    """命中的领域词（保持 DOMAIN_TERMS 的声明顺序，保证确定性）。"""
    lowered = (text or "").lower()
    return [term for term in DOMAIN_TERMS if term in lowered]


# --------------------------------------------------------------------------- 打分


def _idf(doc_freq: Counter[str], total: int) -> dict[str, float]:
    """平滑 IDF。``total`` 为 0 时返回空表（调用方须先处理空语料）。"""
    if total <= 0:
        return {}
    return {
        term: math.log((total + 1) / (df + 1)) + 1.0
        for term, df in doc_freq.items()
    }


def _score_sentences(sentences: list[str], idf: dict[str, float]) -> list[float]:
    """句子打分：Σ IDF(词) / sqrt(词数) × 领域加权。

    除以 ``sqrt(词数)`` 是长度归一化。不归一化的话，长句因为词多必然得分高，
    最后选出来的全是又臭又长的段落 —— 这是 TF-IDF 摘要最典型的失败模式。
    """
    scores: list[float] = []
    for sent in sentences:
        tokens = _sentence_tokens(sent)
        if not tokens:
            scores.append(0.0)
            continue
        base = sum(idf.get(t, 1.0) for t in tokens) / math.sqrt(len(tokens))
        # 命中领域词加权：这是「电力领域要点」与「泛泛而谈」的分界线
        base *= 1.0 + 0.35 * len(_domain_hits(sent))
        scores.append(base)
    return scores


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _mmr_select(
    ranked: list[int],
    scores: list[float],
    token_sets: list[set[str]],
    k: int,
    *,
    lambda_: float = 0.7,
) -> list[int]:
    """最大边际相关（MMR）选句：在「本身重要」与「和已选项不重复」之间取平衡。

    ``value = λ × 归一化分数 − (1−λ) × 与已选句的最大相似度``

    ⚠️ 公式里**必须**带上前半截。只算后半截（冗余度）会让第 2 句往后的选择
    完全由「和上一句不像」决定，把重要性彻底丢掉 —— 结果是为了「不重复」
    选出边缘句子。

    ``lambda_ = 1.0`` 就退化成「直接取分数最高的 k 句」——那样 307 篇新闻里
    前 6 句可能全在讲同一件事（同一个签约仪式的六篇转载），所以默认 0.7。
    """
    picked: list[int] = []
    remaining = list(ranked)
    top = max(scores) if scores else 1.0
    norm = top if top > 0 else 1.0

    while remaining and len(picked) < k:
        best_pos = 0
        best_val = float("-inf")
        for pos, idx in enumerate(remaining):
            if not picked:
                value = scores[idx] / norm
            else:
                redundancy = max(
                    _jaccard(token_sets[idx], token_sets[j]) for j in picked
                )
                value = (lambda_ * scores[idx] / norm) - (1.0 - lambda_) * redundancy
            # 同分时保留先出现的（``remaining`` 已按分数降序初始化）→ 确定性
            if value > best_val:
                best_val = value
                best_pos = pos
        picked.append(remaining.pop(best_pos))
    return picked


def _pick_highlights(
    scored: list[tuple[int, float]],
    picked: set[int],
    entries: list[tuple[int, int, str, bool]],
    docs: list[Doc],
    max_highlights: int,
    *,
    final_text: Callable[[int], tuple[str, tuple[str, ...]]],
) -> list[KeyPoint]:
    """从「含量化数据 / 含进展词」的句子里选亮点。

    两轮扫描：先挑**没被要点占用**的句子，不够再允许复用要点句。
    这样正常情况亮点与要点互补，而句子池很小时也不会返回空列表。
    同一篇最多贡献一条 —— 否则 3 条亮点会全来自同一篇稿子。

    ``final_text`` 由调用方注入：亮点与要点必须走**同一套**补上文逻辑，
    否则同一句话在两处会呈现不同文本。
    """
    out: list[KeyPoint] = []
    seen_docs: set[int] = set()
    for allow_reuse in (False, True):
        for idx, value in scored:
            if len(out) >= max_highlights:
                return out
            if not allow_reuse and idx in picked:
                continue
            doc_i, _si, sent, in_title = entries[idx]
            doc = docs[doc_i]
            if doc.id in seen_docs:
                continue
            seen_docs.add(doc.id)
            text, extra = final_text(idx)
            out.append(KeyPoint(
                text=text,
                article_ids=(doc.id,),
                sources=(doc.source_name,) if doc.source_name else (),
                score=value,
                reasons=_highlight_reasons(text, in_title=in_title) + extra,
            ))
    return out


def _highlight_reasons(sent: str, *, in_title: bool) -> tuple[str, ...]:
    """判定一句话是「亮点」的原因。**显式规则**，不靠模型判断。"""
    reasons: list[str] = []
    if _QUANTITY.search(sent):
        reasons.append("含量化数据")
    hits = [t for t in ACHIEVEMENT_TERMS if t in sent]
    if hits:
        reasons.append(f"含进展词:{'/'.join(hits[:3])}")
    if in_title:
        reasons.append("来自标题")
    return tuple(reasons)


def starts_with_reference(sent: str) -> bool:
    """这句话是否以指代 / 连接词开头（离开上文读不懂）。

    对外暴露是给审计脚本用的 —— 「产出要点不以代词开头」是一条可自动核验的
    质量约束，不该只活在注释里。
    """
    return bool(REFERENCE_LEADS.match((sent or "").strip()))


def _with_prefix(
    sentences: list[str],
    index: int,
    *,
    max_len: int = MAX_POINT_CHARS,
    max_back: int = MAX_CONTEXT_BACK,
) -> tuple[str, int]:
    """给代词开头的句子补齐上文。

    例：``该项目锚定"沙戈荒"大基地调峰电源定位…``
    → ``项目自元月首台机组投产后，9个月内实现4台机组全容量投产，刷新了全国火电项目投产间隔纪录。该项目锚定…``

    为什么不是简单降权了事：这类句子往往**信息量最大**（带量化数据、带结论），
    降权会把最好的内容筛掉；而抽取式的本分就是不改写，所以只能把上文接回来。

    Args:
        sentences: 该篇的正文句列表（与 ``index`` 同一坐标系）。
        index: 目标句下标。
        max_len: 拼接后的长度上界。超了就少补一句，宁可句子长一点，
            也不要为凑完整性把整段搬进来。
        max_back: 最多向前回溯几句（应对「上一句本身也是代词开头」）。

    Returns:
        ``(拼接文本, 实际补入的前缀句数)``。不需要补或补不了时返回 ``(原句, 0)``。
    """
    current = sentences[index]
    if index <= 0 or not starts_with_reference(current):
        return current, 0

    prefix = ""
    used = 0
    cursor = index
    while cursor > 0 and used < max_back:
        prev = sentences[cursor - 1]
        candidate = prev + prefix
        if len(candidate) + len(current) > max_len:
            break
        prefix = candidate
        cursor -= 1
        used += 1
        if not starts_with_reference(prev):
            break

    if not prefix:
        return current, 0
    return prefix + current, used


# --------------------------------------------------------------------------- 主入口


def extract_keywords(docs: list[Doc], k: int = 10) -> list[tuple[str, int, float]]:
    """统计领域关键词：``(词, 出现篇数, 权重)``，按权重降序。

    权重 = 覆盖篇数 × (1 + log(总命中次数))。用**篇数**而不是词频打底，
    是因为一篇长文里「储能」出现 30 次，不该压过 30 篇各提一次「储能」——
    后者才是「这周的焦点」。
    """
    if not docs:
        return []
    docs_with: Counter[str] = Counter()
    total_hits: Counter[str] = Counter()
    for doc in docs:
        text = doc.text.lower()
        seen: set[str] = set()
        for term in DOMAIN_TERMS:
            n = text.count(term)
            if n:
                total_hits[term] += n
                seen.add(term)
        for term in seen:
            docs_with[term] += 1

    ranked = sorted(
        docs_with.items(),
        key=lambda kv: (-(kv[1] * (1.0 + math.log(1 + total_hits[kv[0]]))), kv[0]),
    )
    return [
        (term, n, n * (1.0 + math.log(1 + total_hits[term])))
        for term, n in ranked[:k]
    ]


def _build_headline(
    docs: list[Doc],
    keywords: list[tuple[str, int, float]],
    window_label: str,
    *,
    window_total: int | None = None,
    window_sources: int | None = None,
) -> str:
    """一句总览。不用模型，靠模板 + 关键词 —— 保证离线也有一句人话。

    ``window_total`` / ``window_sources`` 必须是**整个窗口**的统计，
    不是 ``len(docs)``。这两者经常差一个数量级：默认只取 40 篇做摘要，
    而窗口里可能有 307 篇。用 ``len(docs)`` 拼出「共 40 篇」是**说错了话**，
    比不说更糟 —— 用户会以为一周只有 40 条电力新闻。
    """
    total = len(docs) if window_total is None else window_total
    if total <= 0:
        return f"{window_label}没有采集到符合条件的电力新闻。"
    sources = (
        len({d.source_name for d in docs if d.source_name})
        if window_sources is None
        else window_sources
    )
    top = [term for term, _n, _w in keywords[:5]]
    if top:
        focus = "、".join(top)
        return (
            f"{window_label}共 {total} 篇电力新闻，来自 {sources} 个站点，"
            f"焦点集中在：{focus}。"
        )
    return f"{window_label}共 {total} 篇电力新闻，来自 {sources} 个站点。"


def summarize(
    docs: list[Doc],
    *,
    window_label: str = "指定周期",
    window_total: int | None = None,
    window_sources: int | None = None,
    max_points: int = 6,
    max_highlights: int = 3,
    max_keywords: int = 10,
) -> Extraction:
    """对一批新闻做抽取式总结。

    句子池 = 各篇的正文句 + 标题句。标题单独加进来是因为很多站点的正文是
    纯图集或只有一句导语，丢掉标题就没料了。

    Args:
        docs: 参与总结的文章（应由调用方按发布时间倒序传入）。
        window_label: 窗口的人类可读标签，只用于拼 headline。
        window_total: 窗口内**总篇数**（可大于 ``len(docs)``）。用于 headline，
            省略时退化为 ``len(docs)``。
        window_sources: 窗口内站点数，同上。
        max_points: 要点条数上限。
        max_highlights: 亮点条数上限。
        max_keywords: 关键词条数上限。

    Returns:
        :class:`Extraction`。``docs`` 为空时返回空要点 + 一句「没有采集到」。
    """
    head_kw = {"window_total": window_total, "window_sources": window_sources}
    if not docs:
        return Extraction(
            headline=_build_headline(docs, [], window_label, **head_kw),
            key_points=(),
            highlights=(),
            keywords=(),
            doc_count=0,
        )

    keywords = extract_keywords(docs, k=max_keywords)

    # ---- 句子池：正文句 + 标题句 ----
    #: 每篇的正文句（切一次，后面补上文要用同一坐标系）
    sentences_per_doc: list[list[str]] = [split_sentences(d.body) for d in docs]
    #: (doc_index, sent_index, sentence, in_title)。标题句的 sent_index = -1
    #: （它没有上文，也不该去补），正文句的 sent_index 就是它在 sentences_per_doc
    #: 里的下标 —— 补上文靠它定位。
    entries: list[tuple[int, int, str, bool]] = []
    for i, doc in enumerate(docs):
        title = doc.title.strip()
        if len(title) >= MIN_SENT_LEN:
            entries.append((i, -1, title, True))
        for si, sent in enumerate(sentences_per_doc[i]):
            entries.append((i, si, sent, False))

    if not entries:
        # 有文章但全文都是空标题/空正文：如实说，别编一个要点出来
        total = len(docs) if window_total is None else window_total
        return Extraction(
            headline=f"{window_label}采集到 {total} 篇新闻，但正文为空，无法提取要点。",
            key_points=(),
            highlights=(),
            keywords=keywords,
            doc_count=len(docs),
        )

    sentences = [s for _, _si, s, _t in entries]
    token_sets = [_sentence_tokens(s) for s in sentences]
    doc_freq: Counter[str] = Counter()
    for tokens in token_sets:
        doc_freq.update(tokens)
    idf = _idf(doc_freq, len(sentences))

    scores = _score_sentences(sentences, idf)
    # 标题句天然更凝练，给一个固定加成；位置靠前的句子给小幅加成
    for idx, (_i, _si, _s, in_title) in enumerate(entries):
        scores[idx] *= 1.35 if in_title else 1.0
        scores[idx] *= 1.0 + 0.15 * max(0.0, 1.0 - idx / max(1, len(entries)))

    ranked = sorted(range(len(sentences)), key=lambda i: (-scores[i], i))

    def _final_text(entry_index: int) -> tuple[str, tuple[str, ...]]:
        """取出要点文本，必要时补上文。返回 ``(文本, 附加 reasons)``。"""
        doc_i, sent_i, sent, in_title = entries[entry_index]
        if in_title or sent_i < 0:
            return sent, ()
        merged, used = _with_prefix(sentences_per_doc[doc_i], sent_i)
        if used:
            # 明确标出「这句不是原文原样的一句话」，用户要核对时可回原文找
            return merged, ("已补上文",)
        return sent, ()

    # ---- 要点：MMR 去冗余 ----
    picked = _mmr_select(ranked, scores, token_sets, max_points)
    key_points: list[KeyPoint] = []
    for idx in picked:
        doc_i, _si, _sent, in_title = entries[idx]
        doc = docs[doc_i]
        text, extra = _final_text(idx)
        key_points.append(KeyPoint(
            text=text,
            article_ids=(doc.id,),
            sources=(doc.source_name,) if doc.source_name else (),
            score=scores[idx],
            reasons=_highlight_reasons(text, in_title=in_title) + extra,
        ))

    # ---- 亮点：把「含量化数据 / 含进展词」的句子按 (原因数, 分数) 重排 ----
    # 与要点分开选，是为了让亮点能落在没被要点选中、但确实更有信息量的句子上。
    scored_highlights: list[tuple[int, float]] = []
    for idx, (_i, _si, sent, in_title) in enumerate(entries):
        reasons = _highlight_reasons(sent, in_title=in_title)
        if not reasons:
            continue
        # 「含量化数据」比「来自标题」更有价值，所以按原因条数加权
        scored_highlights.append((idx, scores[idx] * (1.0 + 0.5 * len(reasons))))
    scored_highlights.sort(key=lambda p: (-p[1], p[0]))

    highlights = _pick_highlights(
        scored_highlights, set(picked), entries, docs, max_highlights,
        final_text=_final_text,
    )

    return Extraction(
        headline=_build_headline(docs, keywords, window_label, **head_kw),
        key_points=tuple(key_points),
        highlights=tuple(highlights),
        keywords=tuple(keywords),
        doc_count=len(docs),
    )
