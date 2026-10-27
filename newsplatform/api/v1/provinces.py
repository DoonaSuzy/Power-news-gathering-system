"""省份字典与覆盖度接口（改造方案 §3.6 的 ``/api/v1/provinces`` 系列）。

覆盖度口径与 ``newsagg/stats.py::by_province`` 一致，看板与 CLI 不会打架。

路径顺序
--------
``/coverage`` 必须声明在 ``/{code}`` 之前，否则 ``coverage`` 会被当作省份代码。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from newsplatform.core.deps import (
    CHANNEL_DESC,
    ArticleQueryDep,
    Channel,
    CollectorSessionDep,
    ProvinceSessionDep,
    resolve_province_code,
)
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.article_service import ArticleService
from newsplatform.services.channel_service import ChannelService
from newsplatform.services.source_service import ProvinceService

__all__ = ["router"]

router = APIRouter(
    prefix="/provinces",
    tags=["provinces"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)


@router.get("", summary="省级行政区字典（power/province）")
async def list_provinces(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    channel: Annotated[Channel, Query(description=CHANNEL_DESC)] = "all",
) -> dict:
    """省级行政区字典。

    ``all`` / ``power`` 返回电力侧 34 个（31 大陆 + 港澳台），
    ``province`` 返回省级库自带的 31 个大陆省级行政区。
    """
    items = await ChannelService(session, province_session).provinces(channel)
    return ok({"items": items, "total": len(items), "channel": channel})


@router.get("/coverage", summary="省份覆盖度（power/province）")
async def province_coverage(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    channel: Annotated[Channel, Query(description=CHANNEL_DESC)] = "all",
) -> dict:
    """每个省的来源数 / 启用数 / 文章数 / 空正文数 / 最近发布时间。

    含**完全没有数据**的省份（article_count=0），这样看板能直接显示覆盖缺口。
    ``channel=all`` 时以电力侧 34 省为骨架，把省级域的计数**相加**——
    不是取其一，否则「合并后文章数反而变少」会让人以为丢了数据。
    """
    items = await ChannelService(session, province_session).coverage(channel)
    rows = [i.model_dump(mode="json") if hasattr(i, "model_dump") else i for i in items]
    covered = sum(1 for r in rows if r.get("article_count"))
    return ok({
        "items": rows,
        "total": len(rows),
        "covered": covered,
        "missing": len(rows) - covered,
        "channel": channel,
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


@router.get("/{code}/articles", summary="某省文章列表（power/province）")
async def province_articles(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    code: str,
    query: ArticleQueryDep,
) -> dict:
    """等价于 ``GET /articles?province=<code>``，语义更直观。

    路径参数同样接受名称（``/provinces/广东/articles``）。
    ``ArticleQuery`` 是 ``slots=True`` 的 dataclass，没有 ``__dict__``，
    所以必须用 ``dataclasses.replace`` 派生出新实例而不是直接改属性。
    ``channel`` 仍由查询串决定（默认 ``all``），所以这里不覆盖它。
    """
    resolved = await resolve_province_code(session, code)
    page = await ArticleService(session, province_session).list(
        replace(query, province_codes=(resolved,))
    )
    return ok(page.model_dump(mode="json"))
