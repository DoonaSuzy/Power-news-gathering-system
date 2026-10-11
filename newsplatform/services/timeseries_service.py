"""时序数据服务：把「用户写法」翻译成仓储需要的「物理表 + 合法列」。

职责边界
--------
路由只管参数形态，仓储只管 SQL，本层负责三件事：

1. **大区 / 产品解析**——用户可能写「华北」也可能写 ``huabei``，物理表名只有一种；
2. **省份解析**——复用 ``core/provinces``，让「广东」「440000」「华南」都能写；
3. **口径元数据**——单位、中文名、逐日聚合方式随数据一起返回，
   调用方不必再去读 DDL 猜「这个 8.64 是什么单位」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsagg.market.models import DATASETS
from newsagg.weather.models import (
    DAILY_AGG_LABEL,
    DAILY_AGGREGATION,
    GRANULARITIES,
    PRODUCTS,
    REGION_CODES,
    VARIABLE_CN,
    VARIABLE_UNIT,
)
from newsplatform.core.exceptions import BadRequest
from newsplatform.core.provinces import load_rows, resolve_codes
from newsplatform.repositories.timeseries import (
    MarketRepository,
    WeatherRepository,
    province_basis,
    resolve_table,
    select_columns,
)

__all__ = ["TimeSeriesService", "resolve_region"]

#: ASCII 表名段 → 中文大区名（``REGION_CODES`` 的反向索引）。
_CODE_TO_REGION: dict[str, str] = {code: region for region, code in REGION_CODES.items()}

#: 单次响应最多返回多少行时序。气象逐时一个省就上千行，必须有硬上限。
MAX_SERIES_ROWS = 50_000


def resolve_region(value: str) -> str:
    """大区写法归一：中文名或 ``huabei`` 这类 ASCII 码都接受。"""
    key = str(value).strip()
    if key in REGION_CODES:
        return key
    if key.lower() in _CODE_TO_REGION:
        return _CODE_TO_REGION[key.lower()]
    raise BadRequest(
        f"未知大区 {value!r}，可选：{', '.join(REGION_CODES)}（或 {', '.join(_CODE_TO_REGION)}）"
    )


def resolve_product(value: str) -> str:
    key = str(value).strip()
    if key not in PRODUCTS:
        raise BadRequest(f"未知气象产品 {value!r}，可选：{', '.join(PRODUCTS)}")
    return key


class TimeSeriesService:
    """气象 + 行情的时序查询编排。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.weather = WeatherRepository(session)
        self.market = MarketRepository(session)

    # ------------------------------------------------------------------ 气象

    async def weather_series(
        self,
        *,
        region: str,
        product: str,
        granularity: str = "hourly",
        params: list[str] | None = None,
        since: Any = None,
        until: Any = None,
        province: str | None = None,
        station_ids: list[str] | None = None,
        level: str = "station",
        limit: int = 5_000,
        offset: int = 0,
    ) -> dict[str, Any]:
        region_key = resolve_region(region)
        product_key = resolve_product(product)
        if granularity not in GRANULARITIES:
            raise BadRequest(f"granularity 取值必须是 {', '.join(GRANULARITIES)} 之一")
        if level not in ("station", "province"):
            raise BadRequest("level 取值必须是 station 或 province")
        try:
            columns = select_columns(params)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

        table = resolve_table(region_key, product_key)
        province_codes = await resolve_province_codes(self.session, province)
        capped = max(1, min(int(limit), MAX_SERIES_ROWS))

        items = await self.weather.series(
            table=table,
            columns=columns,
            granularity=granularity,
            start=since,
            end=until,
            province_codes=province_codes,
            station_ids=station_ids,
            level=level,
            limit=capped,
            offset=max(0, int(offset)),
        )
        return {
            "meta": {
                "table": table,
                "region": region_key,
                "product": product_key,
                "product_label": PRODUCTS[product_key],
                "granularity": granularity,
                "level": level,
                "columns": list(columns),
                "units": {c: VARIABLE_UNIT[c] for c in columns},
                "labels": {c: VARIABLE_CN[c] for c in columns},
                "daily_basis": (
                    {c: DAILY_AGG_LABEL[c] for c in columns} if granularity == "daily" else {}
                ),
                # 省级汇总口径与站点逐日口径不同（简单圆均值 vs 风速加权），
                # 必须一起返回，否则调用方会把两者当同一个数用。
                "province_basis": (
                    province_basis(columns) if level == "province" else {}
                ),
                "limit": capped,
                "offset": max(0, int(offset)),
                "returned": len(items),
                "truncated": len(items) == capped,
            },
            "items": items,
        }

    async def weather_coverage(self) -> dict[str, Any]:
        """24 张分表的行数与时间跨度——回填进度看板的底。

        表名与 ``(大区, 产品)`` 的对应关系由 :func:`all_tables` 的枚举顺序决定，
        不去反解字符串：``wx_huabei_hist_rt`` 里切出 ``rt`` 当产品名是错的。
        """
        pairs = [(region, product) for region in REGION_CODES for product in PRODUCTS]
        tables = await self.weather.coverage_all()
        return {
            "meta": {"tables": len(tables)},
            "items": [
                {"region": region, "region_code": REGION_CODES[region],
                 "product": product, "product_label": PRODUCTS[product], **t}
                for (region, product), t in zip(pairs, tables, strict=True)
            ],
        }

    @staticmethod
    def weather_params() -> dict[str, Any]:
        """参数目录：列名 / 中文名 / 单位 / 逐日聚合口径。"""
        return {
            "items": [
                {
                    "param": col,
                    "label": VARIABLE_CN[col],
                    "unit": VARIABLE_UNIT[col],
                    "daily_basis": DAILY_AGG_LABEL[col],
                    "daily_kind": DAILY_AGGREGATION[col],
                }
                for col in VARIABLE_CN
            ],
            "granularities": list(GRANULARITIES),
            "products": [
                {"code": code, "label": label} for code, label in PRODUCTS.items()
            ],
            "regions": [
                {"name": region, "code": code} for region, code in REGION_CODES.items()
            ],
        }

    # ------------------------------------------------------------------ 行情

    async def market_series(
        self,
        *,
        dataset: str,
        since: Any = None,
        until: Any = None,
        regions: list[str] | None = None,
        grades: list[str] | None = None,
        limit: int = 2_000,
        offset: int = 0,
    ) -> dict[str, Any]:
        if dataset not in DATASETS:
            raise BadRequest(f"未知数据集 {dataset!r}，可选：{', '.join(DATASETS)}")
        capped = max(1, min(int(limit), MAX_SERIES_ROWS))
        items = await self.market.series(
            dataset=dataset,
            start=since,
            end=until,
            regions=regions,
            grades=grades,
            limit=capped,
            offset=max(0, int(offset)),
        )
        return {
            "meta": {
                "dataset": dataset,
                "label": DATASETS[dataset],
                "limit": capped,
                "offset": max(0, int(offset)),
                "returned": len(items),
                "truncated": len(items) == capped,
            },
            "items": items,
        }

    async def market_coverage(self) -> dict[str, Any]:
        return {"items": await self.market.coverage()}


async def resolve_province_codes(session: AsyncSession, value: str | None) -> list[str] | None:
    """把「广东 / 440000 / 华南 / 全国」解析成省份代码列表。

    ``None`` 表示不加过滤——三态语义见 ``core/provinces.resolve_codes``。
    """
    if value is None or not str(value).strip():
        return None
    return resolve_codes(value, await load_rows(session))
