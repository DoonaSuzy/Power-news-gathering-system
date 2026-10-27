"""文章查询接口（改造方案 §3.6 的 ``/api/v1/articles`` 系列）。

分层约定
--------
路由只做「解析参数 → 调服务 → 包统一响应体」：
- SQL 在 ``repositories/articles.py``
- 业务编排在 ``services/article_service.py``
- 参数校验与依赖注入在 ``core/deps.py``

路径顺序
--------
``/count`` 与 ``/export`` 必须声明在 ``/{article_id}`` **之前**——
FastAPI 按声明顺序匹配，否则 ``count`` 会被当成 ``article_id`` 去转 int 而 422。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from newsplatform.core.deps import (
    ArticleQueryDep,
    Channel,
    CollectorSessionDep,
    ProvinceSessionDep,
)
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.article_service import ArticleService

__all__ = ["router"]

router = APIRouter(
    prefix="/articles",
    tags=["articles"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)


@router.get("", summary="文章列表（分页 · power/province）")
async def list_articles(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    query: ArticleQueryDep,
) -> dict:
    """按省份 / 类别 / 来源 / 时间 / 关键词筛选，返回不含正文全文的列表。

    ``channel`` 决定查哪个数据域（见 :data:`core.deps.CHANNEL_DESC`）。
    ``channel=all`` 时两库各取 ``page*page_size`` 条后归并再切页，
    响应里的 ``channels`` 是**实际**查询的域——省级库没接时会退化成 ``["power"]``。
    """
    page = await ArticleService(session, province_session).list(query)
    return ok(page.model_dump(mode="json"))


@router.get("/count", summary="统计命中条数（power/province）")
async def count_articles(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    query: ArticleQueryDep,
) -> dict:
    """只返回条数，供前端分页器与导出前预览使用。跨域时为两库之和。"""
    total = await ArticleService(session, province_session).count(query)
    return ok({"total": total, "page": query.page, "page_size": query.page_size})


@router.get("/export", summary="导出 CSV（power/province）")
async def export_articles(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    query: ArticleQueryDep,
    response: Response,
    filename: Annotated[str, Query(description="下载文件名（不含扩展名）")] = "powernews_articles",
) -> Response:
    """以 CSV 返回筛选结果（UTF-8 BOM，Excel 双击不乱码）。

    超过 ``EXPORT_MAX_ROWS``（50000）时截断，并在日志与响应头中体现。
    """
    service = ArticleService(session, province_session)
    result = await service.export_csv(query)
    safe = "".join(c for c in filename if c.isalnum() or c in "-_") or "powernews_articles"
    headers = {
        "Content-Disposition": f'attachment; filename="{safe}.csv"',
        "X-Export-Rows": str(result["rows"]),
        "X-Export-Truncated": "1" if result["truncated"] else "0",
    }
    return Response(
        content=result["content"].encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.get("/{article_id}", summary="文章详情（含正文 · power/province）")
async def get_article(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    article_id: int,
    channel: Annotated[Channel, Query(description="数据域；all 时先查电力再查省级")] = "all",
) -> dict:
    """取单篇详情；不存在返回业务码 3003（``article_not_found``）。

    两个数据域的 id 各自自增，**相同 id 不是同一条**，所以详情必须带 ``channel``
    才能保证取到的是你想要的那条。``channel=all`` 下两库都查：只有一条命中就返回
    那一条；**两条都命中返回 409（``ambiguous_article``）**，响应里 ``candidates``
    给出两条的标题/来源/时间，客户端据此带 ``channel`` 重取。
    """
    detail = await ArticleService(session, province_session).detail(article_id, channel)
    return ok(detail.model_dump(mode="json"))
