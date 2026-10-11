"""时序数据访问层（气象 24 张宽表 + 市场行情长表）。

为什么单独一层
--------------
气象按「大区 × 产品」分表，表名是**运行期拼出来的**；行情是单表多数据集。
两者都不在 ORM 里，只能走 ``text()``。SQL 集中在本层，好处是注入面只有一处可审。

白名单
------
表名一律经 :func:`newsagg.weather.models.table_name`（未知大区/产品直接抛错），
列名一律与 ``VALUE_COLUMNS`` 求交。**不做任何「猜列名」的兜底**——
拼错列名的查询不会报错，只会静默返回错列的数据，这是时序接口最坏的失效方式。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from newsagg.market.models import DATASETS
from newsagg.weather.models import (
    GRANULARITIES,
    VALUE_COLUMNS,
    all_tables,
    table_name,
)

__all__ = [
    "MarketRepository",
    "PROVINCE_AGGREGATION",
    "WeatherRepository",
    "province_basis",
    "province_projection",
    "resolve_table",
    "select_columns",
]

#: 省级汇总时**每一列**的口径。
#:
#: 一律 ``AVG`` 是错的，而且是那种「返回了数字所以看起来对」的错：
#: - ``wind_direction_10m``：角度是圆周量，``AVG(350°, 10°)`` 得 180°，
#:   正确值是 0°。必须走圆均值（``ATAN2(AVG(sin), AVG(cos))``）。
#:   注意这是**简单**圆均值，与逐日行里「风速加权圆均值」的站点口径不同，
#:   所以随响应一起返回标签，别让调用方混用。
#: - ``weather_code``：WMO 代码是枚举不是数量，``AVG`` 出来的 5.12 谁也不认。
#:   取 ``MAX``，与逐日口径「当日最严重」同构。
#: - 其余列是线性量，``AVG`` 即省内代表性值（降水量也是均值——按省求和会随
#:   站点数放大，那不是「该省日降水」）。
PROVINCE_AGGREGATION: dict[str, str] = {
    "wind_direction_10m": "circular_mean",
    "weather_code": "max",
}

#: 默认的省级汇总方式。
_DEFAULT_PROVINCE_AGG = "avg"

#: 汇总表达式 → 随响应返回的口径标签。
_PROVINCE_AGG_LABEL = {
    "avg": "省内各站均值",
    "circular_mean": "省内各站简单圆均值（非风速加权）",
    "max": "省内最严重（取最大值）",
}


def province_projection(column: str) -> str:
    """列 → 省级汇总的 SELECT 表达式（别名与列名一致，响应形状保持稳定）。"""
    kind = PROVINCE_AGGREGATION.get(column, _DEFAULT_PROVINCE_AGG)
    if kind == "circular_mean":
        # ATAN2(Y, X)：Y 是 sin 均值、X 是 cos 均值，结果域 (-180, 180]。
        # 必须先 +360 再取模：MySQL 的 MOD() 符号跟随被除数，
        # MOD(-156.27, 360) 仍是 -156.27，值域会漏到负数（实测内蒙古就踩到了）。
        # 用 MOD() 而不是 `%` 运算符：pymysql 会对语句做 %-格式化，
        # 裸 `%` 会直接抛「unsupported format character」。
        return (
            f"ROUND(MOD(DEGREES(ATAN2(AVG(SIN(RADIANS(`{column}`))), "
            f"AVG(COS(RADIANS(`{column}`))))) + 360, 360), 4) AS `{column}`"
        )
    if kind == "max":
        return f"MAX(`{column}`) AS `{column}`"
    return f"ROUND(AVG(`{column}`), 4) AS `{column}`"


def province_basis(columns: Sequence[str]) -> dict[str, str]:
    """列 → 省级汇总口径标签，随响应一起返回。"""
    return {
        c: _PROVINCE_AGG_LABEL[PROVINCE_AGGREGATION.get(c, _DEFAULT_PROVINCE_AGG)]
        for c in columns
    }


def resolve_table(region: str, product: str) -> str:
    """``(大区, 产品)`` → 物理表名；不合法直接 ``ValueError``（不做静默兜底）。"""
    return table_name(region, product)


def select_columns(params: Sequence[str] | None) -> tuple[str, ...]:
    """把用户点名的参数收敛到合法列，并保持建表时的固定顺序。

    未知列名**报错**而不是忽略：忽略会让 ``?params=temprature_2m``（拼错）
    返回「全部 10 列」或「空列」，两种都很难被发现。
    """
    if not params:
        return VALUE_COLUMNS
    wanted = [str(p).strip() for p in params if str(p).strip()]
    unknown = [p for p in wanted if p not in VALUE_COLUMNS]
    if unknown:
        raise ValueError(
            f"未知气象参数 {unknown}，可选：{', '.join(VALUE_COLUMNS)}"
        )
    return tuple(c for c in VALUE_COLUMNS if c in set(wanted))


def _time_filter(
    column: str,
    start: datetime | None,
    end: datetime | None,
    params: dict[str, Any],
) -> list[str]:
    """生成时间区间条件（闭区间），绑定参数写入 ``params``。"""
    clauses: list[str] = []
    if start is not None:
        params["_start"] = start
        clauses.append(f"{column} >= :_start")
    if end is not None:
        params["_end"] = end
        clauses.append(f"{column} <= :_end")
    return clauses


def _in_filter(
    column: str,
    values: Sequence[str] | None,
    prefix: str,
    params: dict[str, Any],
) -> str | None:
    """生成 ``col IN (:p0, :p1...)``，值全部走绑定参数。"""
    if not values:
        return None
    keys = []
    for i, value in enumerate(values):
        key = f"{prefix}{i}"
        params[key] = value
        keys.append(f":{key}")
    return f"{column} IN ({', '.join(keys)})"


class WeatherRepository:
    """``wx_{大区}_{产品}`` 24 张同构宽表的读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def series(
        self,
        *,
        table: str,
        columns: Sequence[str],
        granularity: str,
        start: datetime | None = None,
        end: datetime | None = None,
        province_codes: Sequence[str] | None = None,
        station_ids: Sequence[str] | None = None,
        level: str = "station",
        limit: int = 5000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """取一段时序。``level='province'`` 时按省对同时刻各站求均值。

        ``station`` 层级是原样行；``province`` 层级把同省多个气象站在同一时刻
        上平均——电力侧按省建模时需要的是后者，按站核对时需要前者。
        """
        cols = list(columns)
        params: dict[str, Any] = {}
        where: list[str] = ["granularity = :_gran"]
        params["_gran"] = granularity

        where += _time_filter("ts", start, end, params)
        for clause in (
            _in_filter("province_code", province_codes, "_p", params),
            _in_filter("station_id", station_ids, "_s", params),
        ):
            if clause:
                where.append(clause)

        if level == "province":
            selects = ["ts", "province_code"] + [province_projection(c) for c in cols]
            sql = (
                f"SELECT {', '.join(selects)} FROM `{table}` "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY ts, province_code "
                "ORDER BY province_code, ts "
                f"LIMIT {int(limit)} OFFSET {int(offset)}"
            )
        else:
            selects = ["ts", "station_id", "province_code", "adcode"] + [
                f"`{c}`" for c in cols
            ]
            sql = (
                f"SELECT {', '.join(selects)} FROM `{table}` "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY station_id, ts "
                f"LIMIT {int(limit)} OFFSET {int(offset)}"
            )

        result = await self.session.execute(text(sql), params)
        return [dict(r) for r in result.mappings().all()]

    async def coverage(self, table: str) -> dict[str, Any]:
        """单表覆盖情况：行数、时间跨度、站点数。

        空表与「表不存在」必须区分开——回填进度看板依赖这个判断。
        """
        sql = text(
            f"SELECT COUNT(*) AS rows_total, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            f"COUNT(DISTINCT station_id) AS stations FROM `{table}`"
        )
        row = (await self.session.execute(sql)).mappings().first()
        return {"table": table, **(dict(row or {}) if row else {})}

    async def coverage_all(self) -> list[dict[str, Any]]:
        """24 张表逐一统计。表名来自 :func:`all_tables`，非用户输入。"""
        return [await self.coverage(t) for t in all_tables()]


class MarketRepository:
    """``market_prices`` 单表多数据集的读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def series(
        self,
        *,
        dataset: str,
        start: datetime | None = None,
        end: datetime | None = None,
        regions: Sequence[str] | None = None,
        grades: Sequence[str] | None = None,
        limit: int = 2000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """取某个数据集的一段时序。"""
        if dataset not in DATASETS:
            raise ValueError(f"未知数据集 {dataset!r}，可选：{', '.join(DATASETS)}")

        params: dict[str, Any] = {"_ds": dataset}
        where = ["dataset = :_ds"]
        where += _time_filter("price_date", start, end, params)
        for clause in (
            _in_filter("region", regions, "_r", params),
            _in_filter("grade", grades, "_g", params),
        ):
            if clause:
                where.append(clause)

        sql = (
            "SELECT price_date, region, grade, value, unit, change_value, change_pct, "
            "period, source, source_url FROM market_prices "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY region, grade, price_date "
            f"LIMIT {int(limit)} OFFSET {int(offset)}"
        )
        result = await self.session.execute(text(sql), params)
        return [dict(r) for r in result.mappings().all()]

    async def coverage(self) -> list[dict[str, Any]]:
        """按数据集统计行数与时间跨度（含中文名）。"""
        sql = text(
            "SELECT dataset, COUNT(*) AS rows_total, MIN(price_date) AS first_date, "
            "MAX(price_date) AS last_date, COUNT(DISTINCT region) AS regions, "
            "COUNT(DISTINCT grade) AS grades "
            "FROM market_prices GROUP BY dataset ORDER BY rows_total DESC"
        )
        rows = [dict(r) for r in (await self.session.execute(sql)).mappings().all()]
        known = {r["dataset"]: r for r in rows}
        out: list[dict[str, Any]] = []
        for code, label in DATASETS.items():
            base = known.get(code)
            out.append(
                {
                    "dataset": code,
                    "label": label,
                    "rows_total": (base or {}).get("rows_total", 0),
                    "first_date": (base or {}).get("first_date"),
                    "last_date": (base or {}).get("last_date"),
                    "regions": (base or {}).get("regions", 0),
                    "grades": (base or {}).get("grades", 0),
                }
            )
        return out


#: 供校验「白名单没跑偏」用（也方便 REPL 里直接查）
GRANULARITY_CHOICES = GRANULARITIES
