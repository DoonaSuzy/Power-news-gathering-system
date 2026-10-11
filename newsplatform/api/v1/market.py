"""市场行情时序接口（``market_prices`` 单表多数据集）。

与气象侧同为「时序」，但形状完全不同：气象是 24 张同构宽表按 ``ts`` 排列，
行情是一张长表按 ``dataset / region / grade`` 分类、按 ``price_date`` 排列。
所以两个模块分开，不硬凑成一套参数。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from newsplatform.core.deps import CollectorSessionDep, parse_day
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.timeseries_service import TimeSeriesService

__all__ = ["router"]

router = APIRouter(
    prefix="/market",
    tags=["market"],
    dependencies=[Depends(require_scope("market:read")), Depends(enforce_rate_limit)],
)


def _split(values: list[str] | None) -> list[str] | None:
    """逗号分隔与重复传两种写法等价。"""
    if not values:
        return None
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in str(value).split(",") if part.strip())
    return out or None


@router.get("/datasets", summary="数据集目录与覆盖情况")
async def market_datasets(session: CollectorSessionDep) -> dict:
    """列出 6 个数据集的行数、时间跨度与区域/口径数量；无数据的数据集也在表里。"""
    return ok(await TimeSeriesService(session).market_coverage())


@router.get("/{dataset}", summary="行情时序查询")
async def market_series(
    session: CollectorSessionDep,
    dataset: Annotated[
        str,
        Path(
            description=(
                "数据集：lng_spot / lng_quote / lng_signal / "
                "ceci_import / ceci_pmi / power_spot"
            ),
            examples=["power_spot"],
        ),
    ],
    region: Annotated[list[str] | None, Query(description="区域过滤，逗号分隔或重复传")] = None,
    grade: Annotated[
        list[str] | None, Query(description="口径过滤，如 日前 / 实时 / 到岸标煤单价")
    ] = None,
    since: Annotated[str | None, Query(description="起始业务日期（含）")] = None,
    until: Annotated[str | None, Query(description="结束业务日期（含）")] = None,
    limit: Annotated[int, Query(ge=1, description="最多返回行数，上限 50000")] = 2000,
    offset: Annotated[int, Query(ge=0, description="跳过行数")] = 0,
) -> dict:
    """取某个数据集的一段行情时序（含环比涨跌与出处 URL）。"""
    data = await TimeSeriesService(session).market_series(
        dataset=dataset,
        regions=_split(region),
        grades=_split(grade),
        since=parse_day(since),
        until=parse_day(until, end_of_day=True),
        limit=limit,
        offset=offset,
    )
    return ok(data)
