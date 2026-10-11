"""市场行情入库：把 :class:`~newsagg.market.models.MarketPoint` 幂等写进 ``market_prices``。

为什么单独一层，而不写在采集器里
--------------------------------
1. **采集器要能脱离数据库单测**——喂一段写死 HTML 就能验解析；入库逻辑混进去就
   必须起 MySQL 才能跑测试。
2. 入库要处理幂等、去重、空值这三件与网络无关的事，混在一起会让两边都难改。

幂等语义
--------
唯一键 ``uk_market_point = (dataset, region, grade, price_date, source)``，
配合 ``INSERT ... ON DUPLICATE KEY UPDATE``：

- 同一期数据重复采集 → 覆盖而非追加，行数不变；
- 上游修订历史值（指数编制方常做回溯修正）→ 自动更新，这正是我们要的行为；
- ``created_at`` 不参与 UPDATE，保留首次写入时间；
- ``updated_at`` 由 MySQL 的 ``ON UPDATE CURRENT_TIMESTAMP`` 自动维护。

关于「写入条数」的计数
----------------------
MySQL 对 ``INSERT ... ON DUPLICATE KEY UPDATE`` 的 ``rowcount`` 语义是：

- ``1`` → 新插入
- ``2`` → 命中唯一键且**值有变化**，执行了 UPDATE
- ``0`` → 命中唯一键但值完全没变，什么都没做

所以 :meth:`MarketStore.upsert` 用**逐行** execute 累加这三个计数，而不是
``executemany`` 一把梭——``executemany`` 只回一个总和，没法区分「插了 100 行」
和「更新了 50 行」。行数本身很小（一期周报 20 条、一个月也才几百条），
逐行的额外开销可以忽略，换来的是可信的幂等报告。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from newsagg.db import Database
from newsagg.errors import PowerNewsError
from newsagg.logutil import get_logger
from newsagg.market.models import DATASETS, MarketPoint

__all__ = ["MarketStore", "UpsertResult"]

log = get_logger("market.store")

_INSERT_SQL = """
INSERT INTO market_prices
    (dataset, region, grade, price_date, value, unit,
     change_value, change_pct, period, source, source_url, raw)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    value        = VALUES(value),
    unit         = VALUES(unit),
    change_value = VALUES(change_value),
    change_pct   = VALUES(change_pct),
    period       = VALUES(period),
    source_url   = VALUES(source_url),
    raw          = VALUES(raw)
"""


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """一次入库的结果计数。"""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0

    @property
    def written(self) -> int:
        """实际写进库的行数（新增 + 更新）。"""
        return self.inserted + self.updated

    @property
    def total(self) -> int:
        """处理过的数据点总数（含跳过）。"""
        return self.inserted + self.updated + self.unchanged + self.skipped

    def describe(self) -> str:
        return (
            f"新增 {self.inserted} / 更新 {self.updated} / "
            f"未变 {self.unchanged} / 跳过 {self.skipped}（共 {self.total}）"
        )

    def __add__(self, other: UpsertResult) -> UpsertResult:
        return UpsertResult(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
            skipped=self.skipped + other.skipped,
        )


class MarketStore:
    """``market_prices`` 表的读写门面。

    Args:
        db: 连接工厂。省略时用 :func:`newsagg.config.get_settings` 的默认配置。
    """

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ------------------------------------------------------------------ 写

    @staticmethod
    def _row(point: MarketPoint) -> tuple[Any, ...]:
        return (
            point.dataset,
            point.region,
            point.grade,
            point.price_date,
            point.value,
            point.unit,
            point.change_value,
            point.change_pct,
            point.period,
            point.source,
            point.source_url[:768],  # 列宽 VARCHAR(768)，超了 MySQL 会按严格模式报错
            json.dumps(point.raw, ensure_ascii=False) if point.raw else None,
        )

    def upsert(self, points: list[MarketPoint]) -> UpsertResult:
        """幂等写入一批数据点。

        Args:
            points: 采集器产出的数据点。

        Returns:
            三个计数（新增/更新/未变）与跳过数。

        Raises:
            PowerNewsError: 表不存在（多半是没跑 ``init-db``），或写库失败。
                这里把 PyMySQL 的原始异常包一层，避免把驱动堆栈糊给用户。
        """
        if not points:
            return UpsertResult()

        # 入库前先在内存里查重。**同一批里出现重复唯一键**会让 MySQL 的
        # rowcount 计数失去意义（后一条会以前一条为「已存在」），
        # 而且通常意味着采集器有 bug，值得显式暴露。
        seen: set[tuple[str, str, str, str, str]] = set()
        unique: list[MarketPoint] = []
        duplicates = 0
        for point in points:
            if point.key in seen:
                duplicates += 1
                continue
            seen.add(point.key)
            unique.append(point)
        if duplicates:
            log.warning("同一批内有 %d 个重复唯一键，已只保留首次出现的那条", duplicates)

        result = UpsertResult(skipped=duplicates)
        try:
            conn = self.db.connect()
        except Exception as exc:  # noqa: BLE001 - 连不上统一转成业务异常
            raise PowerNewsError(f"无法连接 MySQL：{exc}") from exc

        try:
            with conn.cursor() as cur:
                for point in unique:
                    # 既无绝对值也无涨跌的数据点不承载任何信息，写进去只会污染统计。
                    if point.value is None and point.change_value is None:
                        result = result + UpsertResult(skipped=1)
                        log.warning("跳过无值数据点：%s", point.label)
                        continue
                    cur.execute(_INSERT_SQL, self._row(point))
                    affected = cur.rowcount
                    if affected == 1:
                        result = result + UpsertResult(inserted=1)
                    elif affected >= 2:
                        result = result + UpsertResult(updated=1)
                    else:
                        result = result + UpsertResult(unchanged=1)
            conn.commit()
        except PowerNewsError:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            message = str(exc)
            if "doesn't exist" in message or "不存在" in message:
                raise PowerNewsError(
                    "market_prices 表不存在——请先执行 `powernews init-db` 建表"
                ) from exc
            raise PowerNewsError(f"写入 market_prices 失败：{exc}") from exc
        finally:
            conn.close()

        log.info("市场行情入库：%s", result.describe())
        return result

    # ------------------------------------------------------------------ 读

    def count(self, *, dataset: str | None = None) -> int:
        """统计行数。"""
        sql = "SELECT COUNT(*) AS n FROM market_prices"
        params: tuple[Any, ...] = ()
        if dataset:
            sql += " WHERE dataset = %s"
            params = (dataset,)
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone() or {}
            return int(row.get("n") or 0)
        finally:
            conn.close()

    def summary(self) -> list[dict[str, Any]]:
        """按数据集汇总：行数、区域数、日期区间、来源。

        这是交付时最有用的一张表——一眼看出三个数据集各覆盖到什么程度。
        """
        sql = """
            SELECT dataset,
                   COUNT(*)                       AS rows_total,
                   COUNT(DISTINCT region)         AS regions,
                   COUNT(DISTINCT source)         AS sources,
                   MIN(price_date)                AS date_from,
                   MAX(price_date)                AS date_to,
                   GROUP_CONCAT(DISTINCT source ORDER BY source) AS source_list
              FROM market_prices
             GROUP BY dataset
             ORDER BY dataset
        """
        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = list(cur.fetchall())
        finally:
            conn.close()
        for row in rows:
            row["dataset_name"] = DATASETS.get(str(row.get("dataset")), "")
        return rows

    def latest(self, *, dataset: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """取最新的若干行（按业务日期倒序）。"""
        sql = """
            SELECT dataset, region, grade, price_date, value, unit,
                   change_value, change_pct, period, source, source_url
              FROM market_prices
        """
        params: list[Any] = []
        if dataset:
            sql += " WHERE dataset = %s"
            params.append(dataset)
        sql += " ORDER BY price_date DESC, dataset, region, grade LIMIT %s"
        params.append(max(1, limit))

        conn = self.db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall())
        finally:
            conn.close()
