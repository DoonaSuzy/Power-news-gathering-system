"""电力新闻智能总结接口（``/api/v1/news/summary``）。

需求对应
--------
| 需求 | 落点 |
|---|---|
| 默认给出**当日**重点新闻汇总 | ``range`` 默认 ``today`` |
| 支持 今日 / 近1周 / 近1个月 / 自定义 | ``range`` 或 ``since``+``until``，口径来自 ``newsagg/timefilter.py`` |
| 核心要点提取 + 亮点提炼 | ``services/summary_service.py``（LLM 或抽取式） |
| 结构清晰、重点突出 | 响应分 ``window`` / ``scope`` / ``stats`` / ``summary`` / ``caveats`` 五段 |

路径为什么单独开在 ``/news`` 而不是 ``/articles/summary``
--------------------------------------------------------
``/articles/{article_id}`` 是个路径参数端点，FastAPI 按声明顺序匹配；把
``/summary`` 挂在它后面会被当成 ``article_id`` 去转 int 而 422。虽然加个
声明顺序的注释也能绕开，但「汇总」与「文章」本就是两类资源，
分开前缀后将来加 ``/news/topics``、``/news/digest`` 也不会再撞。

权限沿用 ``articles:read`` 而不是新开一个 scope：新 scope 会让所有已签发的
API Key 在这个端点上 401，而它们读的本来就是同一批文章。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from newsagg.timefilter import RANGE_LABELS
from newsplatform.core.deps import (
    TIME_RANGE_DESC,
    ArticleQueryDep,
    CollectorSessionDep,
    TimeRange,
    resolve_time_window,
)
from newsplatform.core.exceptions import BadRequest, UpstreamUnavailable
from newsplatform.core.llm import LLMNotConfigured, LLMUnavailable
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.news_search import asearch_electricity_news
from newsplatform.services.summary_service import (
    DEFAULT_MAX_HIGHLIGHTS,
    DEFAULT_MAX_POINTS,
    DEFAULT_SUMMARY_LIMIT,
    MAX_MAX_HIGHLIGHTS,
    MAX_MAX_POINTS,
    MAX_SUMMARY_LIMIT,
    SummaryService,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/news",
    tags=["news"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)

#: 摘要方式。``auto`` 优先 LLM，不可用则降级；``llm`` 强制走模型，
#: 失败直接报错（不静默换实现）；``extractive`` 强制走本地算法（可离线复现）。
SummaryMethod = Literal["auto", "llm", "extractive"]


def _describe_window(range_key: str, date_from: object, date_to: object) -> str:
    """拼人类可读的窗口标签，如 ``近1周（2026-10-06 ~ 2026-10-12）``。

    与 ``newsagg.timefilter.describe_range`` 同风格，但这里用的是**已经解析好**的
    边界（可能来自 since/until），所以不能直接调它。
    """
    label = RANGE_LABELS.get(range_key, range_key)
    if date_from is None or date_to is None:
        return label
    return f"{label}（{date_from:%Y-%m-%d} ~ {date_to:%Y-%m-%d}）"


@router.get("/summary", summary="电力新闻智能总结（核心要点 + 亮点）")
async def news_summary(
    session: CollectorSessionDep,
    query: ArticleQueryDep,
    range_key: Annotated[
        TimeRange | None,
        Query(
            alias="range",
            description=(
                "不传则默认 today（当日汇总）。" + TIME_RANGE_DESC
            ),
            examples=["today"],
        ),
    ] = None,
    since: Annotated[
        str | None,
        Query(description="自定义起始时间（北京时间）。与 range 互斥"),
    ] = None,
    until: Annotated[
        str | None,
        Query(description="自定义结束时间（北京时间，只给日期则含当天）。与 range 互斥"),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1, le=MAX_SUMMARY_LIMIT,
            description=(
                f"参与摘要的文章数上限（默认 {DEFAULT_SUMMARY_LIMIT}）。"
                "它**不**影响 stats.total_matched，后者是整个窗口的真实篇数"
            ),
        ),
    ] = DEFAULT_SUMMARY_LIMIT,
    max_points: Annotated[
        int, Query(ge=1, le=MAX_MAX_POINTS, description="核心要点条数上限"),
    ] = DEFAULT_MAX_POINTS,
    max_highlights: Annotated[
        int, Query(ge=0, le=MAX_MAX_HIGHLIGHTS, description="亮点条数上限，0 表示不要亮点"),
    ] = DEFAULT_MAX_HIGHLIGHTS,
    method: Annotated[
        SummaryMethod,
        Query(
            description=(
                "摘要方式。auto=有 LLM 就用、否则降级；"
                "llm=强制模型（失败即报错）；extractive=强制本地算法"
            )
        ),
    ] = "auto",
) -> dict:
    """返回指定周期内电力领域重点新闻的结构化总结。

    默认档（不传 ``range``）是**今日**。当日无稿时，快捷档会回退到最近有数据的
    一天，并在响应的 ``fallback`` 字段里写明 —— 不会静默把别的日期当成今天。
    自定义档不回退（用户已明确划定范围），只在 ``caveats`` 里提示最近有数据的日期。

    ``method=llm`` 时若 LLM 未配置或调用失败，接口返回错误（400 / 502）而不是
    偷偷换成抽取式；``method=auto``（默认）才会降级，并把降级原因写进 ``caveats``。
    """
    # 默认档：三个时间参数都没给 → today；给了 since/until → custom
    effective_range = range_key
    if effective_range is None:
        effective_range = "custom" if (since or until) else "today"
    key = str(effective_range).strip().lower()

    # 复用与 /articles 完全相同的窗口解析（唯一实现，见 deps.resolve_time_window）
    date_from, date_to = resolve_time_window(
        None if key == "custom" else key,
        since=since,
        until=until,
    )

    scoped = replace(query, date_from=date_from, date_to=date_to)
    service = SummaryService(session)
    try:
        data = await service.news_summary(
            scoped,
            window_label=_describe_window(key, date_from, date_to),
            range_key=key,
            limit=limit,
            max_points=max_points,
            max_highlights=max_highlights,
            method=method,
        )
    except LLMNotConfigured as exc:
        # 客户端要求了一件服务端没配好的事 → 400，而不是 500
        raise BadRequest(str(exc)) from exc
    except LLMUnavailable as exc:
        # 上游模型服务的问题 → 502，让网关/监控能区分「我们挂了」与「模型挂了」
        raise UpstreamUnavailable(f"LLM 总结失败：{exc}") from exc
    finally:
        await service.aclose()

    return ok(data)


@router.get("/live", summary="实时新闻（检索补充，补本地库的时效缺口）")
async def live_news(
    limit: Annotated[
        int, Query(ge=1, le=60, description="返回条数上限"),
    ] = 24,
    queries: Annotated[
        str | None,
        Query(
            description=(
                "自定义检索词，逗号分隔。留空用默认的 6 个时效性主题"
                "（电力新闻 今日 / 电网 最新 动态 / …）"
            )
        ),
    ] = None,
) -> dict:
    """通过 anysearch 检索**最新**电力新闻。

    为什么需要这个端点
    ------------------
    本地采集库有硬缺口：45 个启用源里本机可达仅 12 个，且北极星（bjx）详情页
    被阿里云 WAF 拦，导致**最近几天常常没有新稿**（实测最新停在 10-10）。
    再跑一遍同样的采集窗口补不上 —— 得靠独立检索服务。

    与 ``/articles`` 的区别：那个读本地库（结构化、有时间戳、可回溯正文），
    这个读实时检索（覆盖广、时效新，但只有标题 + 摘要，**没有正文**）。
    两者互补，不互相替代。
    """
    custom = tuple(q.strip() for q in queries.split(",") if q.strip()) if queries else None
    result = await asearch_electricity_news(queries=custom) if custom \
        else await asearch_electricity_news()

    items = [
        {
            "title": doc.title,
            "source": doc.source,
            "url": doc.url,
            "excerpt": doc.excerpt,
            "published": doc.published,
            "origin": doc.origin,
        }
        for doc in result.docs[:limit]
    ]
    return ok({
        "items": items,
        "total": len(result.docs),
        "queries": result.queries,
        "errors": result.errors,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "结果为实时检索，只有标题与摘要，正文请点原文链接查看。",
    })
