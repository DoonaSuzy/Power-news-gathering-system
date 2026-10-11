"""气象数据落库：把 :class:`~newsagg.weather.models.WeatherPoint` 幂等写进 24 张表。

路由规则
--------
每一行按 ``(大区, 产品)`` 决定目标表，大区由 **省份代码** 推导
（``newsagg.provinces`` 是唯一来源）。:meth:`WeatherStore.write` 因此
只在**单表内**做批量 INSERT——跨表混写会让 ``executemany`` 失去意义，
也会让「写进哪张表」这件事从代码里消失。

幂等与计数
----------
主键 ``(station_id, granularity, ts)`` + ``INSERT ... ON DUPLICATE KEY UPDATE``：

- 重复采集同一窗口 → 覆盖，行数不变（这是重跑回填能安全续跑的前提）；
- 上游修订历史值（ERA5 会回溯修正）→ 自动更新，正是想要的行为。

关于计数：``MarketStore`` 那套「逐行 execute 区分 新增/更新/未变」在这里**不适用**——
单次回填是百万行量级，逐行 execute 会让耗时涨一个数量级。所以本层用
``executemany``，只报告**提交行数**与**驱动回报的影响行数**，不假装能区分三者。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from newsagg.db import Database
from newsagg.errors import PowerNewsError
from newsagg.logutil import get_logger
from newsagg.weather.client import Station
from newsagg.weather.models import (
    PRODUCTS,
    REGIONS,
    VALUE_COLUMNS,
    WeatherPoint,
    all_tables,
    table_name,
)

__all__ = ["WriteResult", "WeatherStore", "STATIONS_JSON"]

log = get_logger("weather.store")

ROOT = Path(__file__).resolve().parents[2]
STATIONS_JSON = ROOT / "data" / "weather" / "stations.json"
SCHEMA_SQL = ROOT / "sql" / "weather_schema.sql"

#: 单条 INSERT 的行数。1000 行 × 16 列 ≈ 16k 占位符，远低于 ``max_allowed_packet``
#: 默认 4 MB，同时也是「一次失败只损失 1000 行」的粒度。
ROW_CHUNK = 1000

_INSERT_TMPL = """
INSERT INTO {table}
    (station_id, adcode, province_code, granularity, ts, {cols}, source)
VALUES ({placeholders})
ON DUPLICATE KEY UPDATE
    adcode = VALUES(adcode),
    province_code = VALUES(province_code),
    {updates},
    source = VALUES(source)
"""


def _insert_sql(table: str) -> str:
    cols = ", ".join(VALUE_COLUMNS)
    placeholders = ", ".join(["%s"] * (5 + len(VALUE_COLUMNS) + 1))
    updates = ",\n    ".join(f"{c} = VALUES({c})" for c in VALUE_COLUMNS)
    return _INSERT_TMPL.format(
        table=table, cols=cols, placeholders=placeholders, updates=updates
    )


@dataclass(slots=True)
class WriteResult:
    """一次写入的结果。"""

    rows_submitted: int = 0
    rows_affected: int = 0
    per_table: dict[str, int] = field(default_factory=dict)

    def merge(self, other: WriteResult) -> WriteResult:
        per_table = dict(self.per_table)
        for k, v in other.per_table.items():
            per_table[k] = per_table.get(k, 0) + v
        return WriteResult(
            rows_submitted=self.rows_submitted + other.rows_submitted,
            rows_affected=self.rows_affected + other.rows_affected,
            per_table=per_table,
        )

    def describe(self) -> str:
        return (
            f"提交 {self.rows_submitted} 行 / 影响 {self.rows_affected} 行"
            f"｜涉及 {len(self.per_table)} 张表"
        )


class WeatherStore:
    """24 张气象数据表 + 站点表 + 进度表的读写门面。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ------------------------------------------------------------ 建表

    def ensure_schema(self) -> int:
        """执行 ``sql/weather_schema.sql`` 建表（幂等）。"""
        if not SCHEMA_SQL.exists():
            raise PowerNewsError(
                f"缺少 {SCHEMA_SQL}——请先运行 "
                "`python scripts/gen_weather_schema.py` 生成"
            )
        return self.db.run_sql_file(SCHEMA_SQL)

    # ------------------------------------------------------------ 站点

    def sync_stations(self, path: Path = STATIONS_JSON) -> int:
        """把 ``data/weather/stations.json`` 同步进 ``wx_stations``。"""
        if not path.exists():
            raise PowerNewsError(
                f"缺少 {path}——请先运行 `python scripts/build_weather_stations.py`"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = [
            (
                s["station_id"], int(s["adcode"]), s["name"], s["level"],
                float(s["lon"]), float(s["lat"]), s["province_code"],
                s["province"], s["region"], int(s["is_special"]),
            )
            for s in payload["stations"]
        ]
        sql = """
            INSERT INTO wx_stations
                (station_id, adcode, name, level, lon, lat,
                 province_code, province, region, is_special)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name), level = VALUES(level),
                lon = VALUES(lon), lat = VALUES(lat),
                province_code = VALUES(province_code), province = VALUES(province),
                region = VALUES(region), is_special = VALUES(is_special)
        """
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PowerNewsError(f"同步站点表失败：{exc}") from exc
        finally:
            conn.close()
        log.info("站点主表已同步 %d 行", len(rows))
        return len(rows)

    def load_stations(self, *, regions: Sequence[str] | None = None) -> list[Station]:
        """从 ``wx_stations`` 读站点；``regions`` 为空则取全部。"""
        if regions:
            unknown = set(regions) - set(REGIONS)
            if unknown:
                raise ValueError(f"未知大区：{sorted(unknown)}")
        sql = "SELECT station_id, adcode, lon, lat, province_code FROM wx_stations"
        params: list[Any] = []
        if regions:
            placeholders = ",".join(["%s"] * len(regions))
            sql += f" WHERE region IN ({placeholders})"
            params = list(regions)
        sql += " ORDER BY station_id"

        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            raise PowerNewsError(
                f"读取 wx_stations 失败（是否还没建表？）：{exc}"
            ) from exc
        finally:
            conn.close()
        return [
            Station(
                station_id=str(r["station_id"]),
                adcode=int(r["adcode"]),
                lon=float(r["lon"]),
                lat=float(r["lat"]),
                province_code=str(r["province_code"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------ 写数

    @staticmethod
    def group_by_table(points: Iterable[WeatherPoint]) -> dict[str, list[tuple[Any, ...]]]:
        """按目标表分组并拍平成行元组。"""
        groups: dict[str, list[tuple[Any, ...]]] = {}
        for point in points:
            groups.setdefault(point.table, []).append(point.as_row())
        return groups

    def write(self, points: Iterable[WeatherPoint]) -> WriteResult:
        """幂等写入一批数据点。"""
        groups = self.group_by_table(points)
        if not groups:
            return WriteResult()

        result = WriteResult()
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                for table, rows in groups.items():
                    sql = _insert_sql(table)
                    affected = 0
                    for start in range(0, len(rows), ROW_CHUNK):
                        affected += cur.executemany(sql, rows[start:start + ROW_CHUNK])
                    result = result.merge(WriteResult(
                        rows_submitted=len(rows),
                        rows_affected=affected,
                        per_table={table: len(rows)},
                    ))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            message = str(exc)
            if "doesn't exist" in message or "不存在" in message:
                raise PowerNewsError(
                    "气象数据表不存在——请先执行 `powernews weather init` 建表"
                ) from exc
            raise PowerNewsError(f"写入气象数据失败：{exc}") from exc
        finally:
            conn.close()

        log.info("气象数据入库：%s", result.describe())
        return result

    # ------------------------------------------------------------ 进度

    def mark_progress(
        self,
        *,
        product: str,
        region: str,
        period_start: Any,
        period_end: Any,
        hourly_rows: int,
        daily_rows: int,
        stations: int,
        status: str = "ok",
        note: str = "",
    ) -> None:
        """记录一片工作单元的完成状态，供长回填续跑。"""
        sql = """
            INSERT INTO wx_ingest_progress
                (product, region, period_start, period_end,
                 hourly_rows, daily_rows, stations, status, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                hourly_rows = VALUES(hourly_rows),
                daily_rows  = VALUES(daily_rows),
                stations    = VALUES(stations),
                status      = VALUES(status),
                note        = VALUES(note)
        """
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    product, region, period_start, period_end,
                    hourly_rows, daily_rows, stations, status, note[:255],
                ))
            conn.commit()
        finally:
            conn.close()

    def completed_periods(self, *, product: str, region: str) -> set[tuple[Any, Any]]:
        """已成功完成的窗口集合，用于跳过。"""
        sql = """
            SELECT period_start, period_end FROM wx_ingest_progress
             WHERE product = %s AND region = %s AND status = 'ok'
        """
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (product, region))
                rows = list(cur.fetchall())
        finally:
            conn.close()
        return {(r["period_start"], r["period_end"]) for r in rows}

    # ------------------------------------------------------------ 读

    def summary(self) -> list[dict[str, Any]]:
        """逐表行数与时间覆盖——交付时最有用的一张表。"""
        out: list[dict[str, Any]] = []
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                for region in REGIONS:
                    for product in PRODUCTS:
                        table = table_name(region, product)
                        cur.execute(
                            f"""
                            SELECT COUNT(*) AS n,
                                   COUNT(DISTINCT station_id) AS stations,
                                   MIN(ts) AS ts_from, MAX(ts) AS ts_to
                              FROM {table}
                            """
                        )
                        row = cur.fetchone() or {}
                        out.append({
                            "table": table,
                            "region": region,
                            "product": product,
                            "rows": int(row.get("n") or 0),
                            "stations": int(row.get("stations") or 0),
                            "ts_from": row.get("ts_from"),
                            "ts_to": row.get("ts_to"),
                        })
        finally:
            conn.close()
        return out

    def total_rows(self) -> int:
        """24 张表的总行数。"""
        return sum(item["rows"] for item in self.summary())

    def missing_tables(self) -> list[str]:
        """列出尚未建立的表（用于自检）。"""
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name AS t FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
                existing = {str(r["t"]) for r in cur.fetchall()}
        finally:
            conn.close()
        return [t for t in all_tables() if t not in existing]
