"""省份字典与覆盖度接口（改造方案 §3.6 的 ``/api/v1/provinces`` 系列）。

覆盖度口径与 ``newsagg/stats.py::by_province`` 一致，看板与 CLI 不会打架。

路径顺序
--------
``/coverage`` 必须声明在 ``/{code}`` 之前，否则 ``coverage`` 会被当作省份代码。
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends

from newsplatform.core.deps import (
    ArticleQueryDep,
    CollectorSessionDep,
    resolve_province_code,
)
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.article_service import ArticleService
from newsplatform.services.source_service import ProvinceService

__all__ = ["router"]

router = APIRouter(
    prefix="/provinces",
    tags=["provinces"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)


@router.get("", summary="省级行政区字典")
async def list_provinces(session: CollectorSessionDep) -> dict:
    """全部 34 个省级行政区（31 大陆 + 港澳台），按行政区划代码排序。"""
    items = await ProvinceService(session).list()
    return ok({"items": [i.model_dump(mode="json") for i in items], "total": len(items)})


@router.get("/coverage", summary="省份覆盖度")
async def province_coverage(session: CollectorSessionDep) -> dict:
    """每个省的来源数 / 启用数 / 文章数 / 空正文数 / 最近发布时间。

    含**完全没有数据**的省份（article_count=0），这样看板能直接显示覆盖缺口。
    """
    items = await ProvinceService(session).coverage()
    rows = [i.model_dump(mode="json") for i in items]
    covered = sum(1 for r in rows if r.get("article_count"))
    return ok({
        "items": rows,
        "total": len(rows),
        "covered": covered,
        "missing": len(rows) - covered,
    })


@router.get("/{code}", summary="省份详情")
async def get_province(session: CollectorSessionDep, code: str) -> dict:
    """取单个省份。

    路径参数既可以写代码，也可以写名称（``/provinces/440000`` 与
    ``/provinces/广东`` 等价）。写大区名（如 ``华南``）会报 400——
    一个接口只能查一个省，含糊地取第一个会让人以为查对了。
    不存在返回业务码 3004。
    """
    resolved = await resolve_province_code(session, code)
    item = await ProvinceService(session).get(resolved)
    return ok(item.model_dump(mode="json"))


@router.get("/{code}/articles", summary="某省文章列表")
async def province_articles(
    session: CollectorSessionDep, code: str, query: ArticleQueryDep
) -> dict:
    """等价于 ``GET /articles?province=<code>``，语义更直观。

    路径参数同样接受名称（``/provinces/广东/articles``）。
    ``ArticleQuery`` 是 ``slots=True`` 的 dataclass，没有 ``__dict__``，
    所以必须用 ``dataclasses.replace`` 派生出新实例而不是直接改属性。
    """
    resolved = await resolve_province_code(session, code)
    page = await ArticleService(session).list(replace(query, province_codes=(resolved,)))
    return ok(page.model_dump(mode="json"))
