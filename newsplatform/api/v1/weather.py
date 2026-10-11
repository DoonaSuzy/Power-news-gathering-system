"""气象时序接口（24 张「大区 × 产品」分表的统一读取入口）。

路径形态
--------
``GET /api/v1/weather/{region}/{product}``——大区和产品是**固定枚举**，
放在路径里比 query 参数更好：SDK、网关限流、访问日志都能按维度直接统计。
``/coverage`` 与 ``/params`` 必须声明在 ``/{region}/{product}`` **之前**，
否则会被当成 ``region='coverage'`` 去解析而 400。

为什么不返回 ORM 对象
---------------------
分表是运行期拼名，列也是用户点名的子集，形状本身是动态的——
统一 ``{"meta": ..., "items": [...]}`` 让调用方拿到口径说明一起走。
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
    prefix="/weather",
    tags=["weather"],
    dependencies=[Depends(require_scope("weather:read")), Depends(enforce_rate_limit)],
)


def _split(values: list[str] | None) -> list[str] | None:
    """把 ``?params=a,b`` 与 ``?params=a&params=b`` 两种写法统一成列表。"""
    if not values:
        return None
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in str(value).split(",") if part.strip())
    return out or None


@router.get("/coverage", summary="24 张分表的覆盖情况")
async def weather_coverage(session: CollectorSessionDep) -> dict:
    """每张表的行数、时间跨度与站点数——回填进度与漏采看板的底。"""
    return ok(await TimeSeriesService(session).weather_coverage())


@router.get("/params", summary="气象参数目录（含单位与逐日口径）")
async def weather_params() -> dict:
    """列出 10 个参数的中英文名、单位，以及逐日行的聚合方式。

    逐日聚合已改为本机计算（不再请求上游 ``daily``），口径必须能被调用方查到，
    否则「日累计辐射」这类同名列会被当成日均值用。
    """
    return ok(TimeSeriesService.weather_params())


@router.get("/{region}/{product}", summary="气象时序查询")
async def weather_series(
    session: CollectorSessionDep,
    region: Annotated[str, Path(description="大区：华北 或 huabei", examples=["华北"])],
    product: Annotated[
        str, Path(description="产品：hist_rt / hist_fc / daily_fc", examples=["hist_rt"])
    ],
    granularity: Annotated[
        str, Query(description="粒度：hourly（逐时）或 daily（逐日）")
    ] = "hourly",
    params: Annotated[
        list[str] | None,
        Query(
            description="参数列，逗号分隔或重复传；缺省返回全部 10 个",
            examples=["temperature_2m"],
        ),
    ] = None,
    province: Annotated[
        str | None, Query(description="省份过滤：代码 / 全称 / 简称 / 大区名")
    ] = None,
    station_id: Annotated[
        list[str] | None, Query(description="站点 id，逗号分隔或重复传")
    ] = None,
    level: Annotated[
        str, Query(description="station=原样行；province=同省各站同时刻均值")
    ] = "station",
    since: Annotated[str | None, Query(description="起始时间（含）")] = None,
    until: Annotated[str | None, Query(description="结束时间（含；只给日期则到当天 23:59:59）")] = None,
    limit: Annotated[int, Query(ge=1, description="最多返回行数，上限 50000")] = 5000,
    offset: Annotated[int, Query(ge=0, description="跳过行数")] = 0,
) -> dict:
    """按「大区 + 产品 + 粒度」取一段气象时序。"""
    data = await TimeSeriesService(session).weather_series(
        region=region,
        product=product,
        granularity=granularity,
        params=_split(params),
        province=province,
        station_ids=_split(station_id),
        level=level,
        since=parse_day(since),
        until=parse_day(until, end_of_day=True),
        limit=limit,
        offset=offset,
    )
    return ok(data)
