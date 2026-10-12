"""电力新闻智能总结服务：编排「取数 → 摘要 → 组装响应」。

两条摘要路径（见 ``services/extractive.py`` 的模块说明）
--------------------------------------------------------
- ``llm``：调大模型做抽象式总结，能改写、能归纳，但需要配置且可能失败；
- ``extractive``：本仓库自带的确定性算法，永远可用。

``method=auto``（默认）会优先试 LLM，失败则降级到抽取式，并把
**为什么降级**写进响应 ``caveats`` —— 用户看到的每个数字和每句话，
都要能解释它是怎么来的。

一个必须显式处理的边界
----------------------
``range=today`` 是产品默认档，但凌晨或源站当天未发稿时它命中 0 篇。
返回一个空汇总对「看看最近有什么重点」毫无帮助；而**静默**把它换成
「最近有数据的一天」又会让用户以为今天真有这么多新闻。所以：
只有快捷档才允许回退，且响应里带 ``fallback`` 说明；自定义档一律不回退
（用户已经明确划了范围），只在 ``caveats`` 里提示最近有数据的日期。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from newsagg.logutil import get_logger
from newsagg.timefilter import RANGE_DAYS, RANGE_LABELS, end_of_day, start_of_day
from newsplatform.core.llm import LLMClient, LLMUnavailable, build_llm_client
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository
from newsplatform.services.extractive import Doc, summarize

__all__ = ["SummaryMethod", "SummaryService", "build_llm_user_prompt", "SUMMARY_SYSTEM_PROMPT"]

log = get_logger("newsplatform.summary")

#: 允许的摘要方式
SummaryMethod = str  # "auto" | "llm" | "extractive"

#: 送入摘要的文章数上限。40 篇足以覆盖一个白天的主要发布，再多的边际信息
#: 极低，而抽取式打分的代价随篇数线性上升。
DEFAULT_SUMMARY_LIMIT: Final[int] = 40
MAX_SUMMARY_LIMIT: Final[int] = 200

#: 默认要点 / 亮点条数
DEFAULT_MAX_POINTS: Final[int] = 6
DEFAULT_MAX_HIGHLIGHTS: Final[int] = 3
MAX_MAX_POINTS: Final[int] = 20
MAX_MAX_HIGHLIGHTS: Final[int] = 10


SUMMARY_SYSTEM_PROMPT = """你是电力行业新闻分析师。用户会给你一批同一时间窗口内的电力新闻，
你要提炼出这一周期的核心要点与亮点。

要求：
1. 只依据给定材料，不得引入材料外的信息，不做推测性判断。
2. 合并讲同一件事的多篇稿件，不要为同一事件重复出要点。
3. 每条要点是一句完整、可独立阅读的中文陈述句，不超过 60 字，具体到主体与动作，
   例如「国家电网在江苏投运±800千伏特高压直流工程，新增外送能力 800 万千瓦」。
   避免「某公司召开会议」这类没有信息量的表述。
4. 亮点从材料里挑选**有实料**的条目：含量化数据、有明确进展（投产/开工/并网/中标/
   首次）或是政策口径变化的。
5. article_ids 只能使用材料中出现过的编号，不得编造。

只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏：
{
  "headline": "一句话总览这个周期的电力新闻焦点",
  "key_points": [{"text": "要点正文", "article_ids": [1, 2]}],
  "highlights": [{"text": "亮点正文", "article_ids": [3]}]
}"""


def _fmt(value: datetime | None) -> str | None:
    """时间统一按北京时间 naive 输出，与 ``published_at_cst`` 同口径。"""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def build_llm_user_prompt(
    docs: list[Doc],
    *,
    window_label: str,
    max_points: int,
    max_highlights: int,
    max_chars: int,
) -> str:
    """把文章列表拼成给模型的 user 消息。

    纯函数，便于单测（不需要网络）。``max_chars`` 是**硬上限**：超出就按顺序截断
    并显式告诉模型「材料被截断」——不告诉它的话，模型会把「没提供」当成
    「材料里没有」，进而漏掉后半程的重点还说得理直气壮。
    """
    header = (
        f"时间窗口：{window_label}\n"
        f"材料共 {len(docs)} 篇电力新闻。\n"
        f"请输出 {max_points} 条以内的核心要点、{max_highlights} 条以内的亮点。\n"
    )
    parts: list[str] = []
    used = len(header)
    truncated = 0
    for doc in docs:
        block = (
            f"\n[{doc.id}] {doc.title}\n"
            f"来源：{doc.source_name or '未知'}"
            f"｜时间：{_fmt(doc.published_at) or '未知'}\n"
            f"{doc.body.strip()[:600]}\n"
        )
        if used + len(block) > max_chars:
            truncated = len(docs) - len(parts)
            break
        parts.append(block)
        used += len(block)

    joined = "".join(parts)
    tail = ""
    if truncated:
        tail = (
            f"\n\n（注意：为控制长度，材料只提供了前 {len(parts)} 篇，"
            f"另有 {truncated} 篇未提供。请只针对已提供材料总结，不要臆测其余内容。）"
        )
    return header + joined + tail


class SummaryService:
    """电力新闻智能总结。

    Args:
        session: 采集库只读会话。
        llm: 注入的 LLM 客户端；省略时按中台配置构造。
    """

    def __init__(self, session: AsyncSession, *, llm: LLMClient | None = None) -> None:
        self.session = session
        self.repo = ArticleRepository(session)
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        """惰性构造：没配 LLM 的部署连这个对象都不会被创建。"""
        if self._llm is None:
            self._llm = build_llm_client()
        return self._llm

    async def aclose(self) -> None:
        """释放 LLM 的连接池。没构造过就什么都不做（不凭空建一个再关）。"""
        if self._llm is not None:
            await self._llm.aclose()

    # ------------------------------------------------------------------ 主入口
    async def news_summary(
        self,
        query: ArticleQuery,
        *,
        window_label: str,
        range_key: str | None = "today",
        limit: int = DEFAULT_SUMMARY_LIMIT,
        max_points: int = DEFAULT_MAX_POINTS,
        max_highlights: int = DEFAULT_MAX_HIGHLIGHTS,
        method: str = "auto",
    ) -> dict[str, Any]:
        """生成指定时间窗口的电力新闻总结。

        Args:
            query: 已经解析过时间窗口的查询条件（含 ``date_from``/``date_to``）。
            window_label: 人类可读的窗口标签，如 ``近1周（2026-10-06 ~ 2026-10-12）``。
            range_key: 快捷档键名，用于判断是否允许「空窗口回退」。
            limit: 参与摘要的文章数上限。
            max_points: 要点条数上限。
            max_highlights: 亮点条数上限。
            method: ``auto`` / ``llm`` / ``extractive``。

        Returns:
            可直接序列化的响应字典。
        """
        caveats: list[str] = []
        fallback: dict[str, Any] | None = None

        stats = await self.repo.window_stats(query)
        effective_label = window_label

        # ---- 空窗口处理 ----
        if int(stats.get("total") or 0) == 0:
            found = await self.repo.latest_window_with_data(query, days=1)
            if found is None:
                caveats.append("该筛选条件下全库没有任何带发布时间的文章，无法生成总结。")
            elif range_key in (None, "custom"):
                # 自定义档：用户划定的范围不能动，只给出线索
                caveats.append(
                    f"自定义窗口内没有文章。该筛选条件下最近一次有数据的日期是 "
                    f"{found:%Y-%m-%d}，可传 since={found:%Y-%m-%d}&until={found:%Y-%m-%d} 查询。"
                )
            else:
                # 快捷档：回退到最近有数据的一天，并显式标注
                day = found.date()
                query = self._retarget(query, start_of_day(day), end_of_day(day))
                label = RANGE_LABELS.get(str(range_key), str(range_key))
                effective_label = f"{label}（空）→ 回退到最近有数据的一天（{day:%Y-%m-%d}）"
                fallback = {
                    "applied": True,
                    "reason": f"「{label}」窗口内 0 篇，已回退到最近有数据的 {day:%Y-%m-%d}",
                    "original_window": {
                        "start": _fmt(query.date_from),
                        "end": _fmt(query.date_to),
                    },
                    "effective_window": {
                        "start": _fmt(start_of_day(day)),
                        "end": _fmt(end_of_day(day)),
                    },
                    "latest_article_at": _fmt(found),
                }
                caveats.append(
                    f"注意：请求的是「{label}」，但该窗口内没有新闻，"
                    f"以下内容来自最近有数据的 {day:%Y-%m-%d}。"
                )
                stats = await self.repo.window_stats(query)
                log.info("总结窗口回退：%s → %s", window_label, day)

        # ---- 取数 ----
        # 整个窗口 0 篇（且没能回退）时就不必再查一次了：``list_for_summary``
        # 与 ``window_stats`` 用的是同一套条件，左连接省份表也不会减少行数，
        # 那次查询**必然**返回空。省掉它，同时让下面的空结果路径更快。
        total = int(stats.get("total") or 0)
        rows = await self.repo.list_for_summary(query, limit=limit) if total > 0 else []
        docs = [
            Doc(
                id=int(r["id"]),
                title=str(r.get("title") or ""),
                body=str(r.get("body_text") or ""),
                source_name=str(r.get("source_name") or ""),
                published_at=r.get("published_at_cst"),
                url=str(r.get("url") or ""),
                province_name=r.get("province_name"),
            )
            for r in rows
        ]

        # ---- 摘要 ----
        payload, use_method = await self._run_summary(
            docs,
            method=method,
            window_label=effective_label,
            window_total=int(stats.get("total") or 0),
            window_sources=int(stats.get("sources") or 0),
            max_points=max_points,
            max_highlights=max_highlights,
            caveats=caveats,
        )

        return {
            "window": {
                "range": range_key,
                "label": effective_label,
                "start": _fmt(query.date_from),
                "end": _fmt(query.date_to),
                "days": self._window_days(range_key, query),
            },
            "scope": {
                "province_codes": list(query.province_codes) if query.province_codes else None,
                "category": query.category,
                "adapter_name": query.adapter_name,
                "source_id": query.source_id,
                "keyword": query.keyword,
                "exclude_duplicates": query.exclude_duplicates,
            },
            "stats": {
                "total_matched": int(stats.get("total") or 0),
                "distinct_sources": int(stats.get("sources") or 0),
                "distinct_provinces": int(stats.get("provinces") or 0),
                "earliest": _fmt(stats.get("earliest")),
                "latest": _fmt(stats.get("latest")),
                "articles_used": len(docs),
                # 参与摘要的文章覆盖了几个自然日。**这是样本代表性的自证**：
                # 「近1个月」只覆盖 1 天就说明抽样退化了（曾经真的这样）。
                "sampled_days": len(
                    {d.published_at.date() for d in docs if d.published_at}
                ),
            },
            "summary": payload,
            "method": use_method,
            "llm": self.llm.settings.describe(),
            "fallback": fallback,
            "caveats": caveats,
            "representative_articles": self._representative(docs, payload),
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _retarget(q: ArticleQuery, date_from: datetime, date_to: datetime) -> ArticleQuery:
        """派生一个换了时间窗的新查询（``ArticleQuery`` 是 slots dataclass）。"""
        return replace(q, date_from=date_from, date_to=date_to)

    @staticmethod
    def _window_days(range_key: str | None, q: ArticleQuery) -> int | None:
        """窗口天数。快捷档直接查表；自定义档按日期差算，缺边界时返回 None。"""
        key = str(range_key or "").strip().lower()
        if key in RANGE_DAYS:
            return RANGE_DAYS[key]
        if q.date_from and q.date_to:
            return (q.date_to.date() - q.date_from.date()).days + 1
        return None

    async def _run_summary(
        self,
        docs: list[Doc],
        *,
        method: str,
        window_label: str,
        window_total: int,
        window_sources: int,
        max_points: int,
        max_highlights: int,
        caveats: list[str],
    ) -> tuple[dict[str, Any], str]:
        """跑摘要，返回 ``(payload, 实际使用的方法)``。

        ``window_total`` / ``window_sources`` 是**整个窗口**的统计，必须一路传到
        ``summarize``：headline 里说「共 N 篇」的 N 是窗口篇数，不是参与摘要的
        篇数（默认只取 40 篇）。用错会把一周 307 篇说成 40 篇。
        """
        normalized = (method or "auto").strip().lower()
        want_llm = normalized in ("auto", "llm")
        head_kw = {"window_total": window_total, "window_sources": window_sources}

        if not docs:
            extraction = summarize(
                docs, window_label=window_label,
                max_points=max_points, max_highlights=max_highlights, **head_kw,
            )
            return {
                "headline": extraction.headline,
                "key_points": [],
                "highlights": [],
                "keywords": [],
                "doc_count": 0,
            }, "extractive"

        if want_llm and self.llm.configured:
            try:
                return await self._llm_summary(
                    docs, window_label=window_label, window_total=window_total,
                    max_points=max_points, max_highlights=max_highlights,
                ), "llm"
            except LLMUnavailable as exc:
                if normalized == "llm":
                    # 用户显式要求 LLM：失败就该报出来，不能偷偷换成抽取式
                    raise
                caveats.append(f"LLM 调用失败，已降级为抽取式总结：{exc}")
                log.warning("LLM 总结失败，降级抽取式：%s", exc)
        elif want_llm:
            reason = (
                "未启用 LLM（POWERNEWS_PLATFORM_LLM_ENABLED 未开）"
                if not self.llm.settings.enabled
                else "LLM 配置不完整（缺 POWERNEWS_PLATFORM_LLM_API_KEY / BASE_URL / MODEL）"
            )
            if normalized == "llm":
                from newsplatform.core.llm import LLMNotConfigured

                raise LLMNotConfigured(f"method=llm 但未配置 LLM：{reason}")
            caveats.append(f"{reason}，本次使用抽取式总结。")

        extraction = summarize(
            docs, window_label=window_label,
            max_points=max_points, max_highlights=max_highlights, **head_kw,
        )
        return extraction.as_dict(), "extractive"

    async def _llm_summary(
        self,
        docs: list[Doc],
        *,
        window_label: str,
        window_total: int,
        max_points: int,
        max_highlights: int,
    ) -> dict[str, Any]:
        """调 LLM 并**校验**它的输出。

        ``headline`` 仍由本地模板生成而不是采信模型那句话：它要写「共 N 篇」，
        而 N 是窗口总数 —— 模型看不到这个数（材料被截断时它连材料有多少篇都不知道），
        让它写必然出现「共 40 篇」这种错。模型的语言能力用在要点上更值。
        """
        prompt = build_llm_user_prompt(
            docs,
            window_label=window_label,
            max_points=max_points,
            max_highlights=max_highlights,
            max_chars=self.llm.settings.max_input_chars,
        )
        raw = await self.llm.chat_json(system=SUMMARY_SYSTEM_PROMPT, user=prompt)
        known = {doc.id: doc for doc in docs}
        local = summarize(
            docs, window_label=window_label, window_total=window_total,
            max_points=max_points, max_highlights=max_highlights,
        )
        return {
            "headline": local.headline,
            "model_headline": str(raw.get("headline") or "").strip() or None,
            "key_points": self._clean_points(raw.get("key_points"), known, max_points),
            "highlights": self._clean_points(raw.get("highlights"), known, max_highlights),
            "keywords": local.as_dict()["keywords"],
            "doc_count": len(docs),
        }

    @staticmethod
    def _clean_points(
        raw: Any, known: dict[int, Doc], cap: int
    ) -> list[dict[str, Any]]:
        """清洗模型给的要点列表。

        **必须过滤 article_ids**：模型会编造编号（幻觉引用）。这里只保留
        材料里真实存在的 id；一条要点若引用全都不存在，就把它降级成
        「无出处要点」而不是丢弃 —— 内容可能仍有用，但用户能看到它没有出处。
        """
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw[:cap]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            ids_raw = item.get("article_ids")
            ids: list[int] = []
            if isinstance(ids_raw, list):
                for value in ids_raw:
                    try:
                        candidate = int(value)
                    except (TypeError, ValueError):
                        continue
                    if candidate in known and candidate not in ids:
                        ids.append(candidate)
            out.append({
                "text": text,
                "article_ids": ids,
                "sources": [
                    known[i].source_name for i in ids if known[i].source_name
                ],
                "score": None,
                "reasons": [] if ids else ["模型未给出有效出处"],
            })
        return out

    @staticmethod
    def _representative(docs: list[Doc], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """被要点/亮点引用到的文章清单（按引用顺序去重）。

        给「要点」配可点进去的原文，否则用户没法核实总结对不对。
        """
        order: list[int] = []
        for point in list(payload.get("key_points") or []) + list(payload.get("highlights") or []):
            for aid in point.get("article_ids") or []:
                if aid not in order:
                    order.append(int(aid))
        by_id = {doc.id: doc for doc in docs}
        out: list[dict[str, Any]] = []
        for aid in order:
            doc = by_id.get(aid)
            if doc is None:
                continue
            out.append({
                "id": doc.id,
                "title": doc.title,
                "source_name": doc.source_name,
                "province_name": doc.province_name,
                "published_at": _fmt(doc.published_at),
                "url": doc.url,
                "excerpt": doc.body[:120],
            })
        return out
