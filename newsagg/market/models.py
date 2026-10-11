"""市场行情数据点的统一结构。

三类数据（LNG 现价 / 电煤进口指数 / 电力现货价）来源不同、字段各异，
但都能归一到同一个形状：**某个日期、某个区域、某个口径下的一个数值**。
这个共同形状就是 :class:`MarketPoint`。

分工（与新闻采集的 `session` / `spider` 分工同构）：

- ``fetchers/*`` 只负责「抓 + 解析」，产出 ``MarketPoint`` 列表，**不碰数据库**；
- ``store`` 负责幂等入库；
- ``cli`` 负责编排。

这样解析逻辑可以脱离数据库单测——喂一段写死的 HTML 就能验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = ["DATASETS", "SIGNED_DATASETS", "MarketPoint"]

#: 数据集标识 → 中文名。新增数据集时必须同步这里与 ``sql/schema.sql`` 的表注释。
DATASETS: dict[str, str] = {
    "lng_spot": "液化天然气现价",
    "lng_quote": "液化天然气挂牌报价明细",
    "lng_signal": "液化天然气均差信号",
    "ceci_import": "中国电煤采购价格指数（进口）",
    "ceci_pmi": "中国电煤采购经理人指数",
    "power_spot": "电力现货市场价格",
}

#: 取值可能为负的数据集。
#:
#: 存在的理由：``lng_signal`` 存的是「10日均线 − 20日均线」的**价差**，
#: 它天然有正有负。若把它和价格混在同一个数据集里，``AVG(value)`` 这类聚合
#: 就会被负值悄悄拉偏。所以这里显式声明，供校验脚本区分「价格」与「信号」。
SIGNED_DATASETS: frozenset[str] = frozenset({"lng_signal"})


@dataclass(frozen=True, slots=True)
class MarketPoint:
    """一个市场行情数据点。

    Attributes:
        dataset: 数据集标识，取值见 :data:`DATASETS`。
        region: 区域或省份；**全国口径用 ``CN``**（不用 NULL，避免 NULL 语义歧义）。
        grade: 规格或口径，如 ``日前`` / ``实时`` / ``到岸标煤单价``。
        price_date: 业务日期（数据所属日期，不是采集时间）。
        value: 价格或指数值。允许为 ``None``——有些源只公布涨跌不公布绝对值。
        unit: 单位，如 ``元/吨``。
        change_value: 环比涨跌绝对值。
        change_pct: 环比涨跌幅（百分数，如 ``0.6`` 表示 0.6%）。
        period: 期次或周期标识，如 ``2026年第34期``。
        source: 来源标识，如 ``cqpgx``。
        source_url: 数据出处 URL，便于回溯核对。
        raw: 原始字段快照（入库时序列化为 JSON）。
    """

    dataset: str
    region: str
    grade: str
    price_date: date
    value: float | None
    unit: str
    source: str
    change_value: float | None = None
    change_pct: float | None = None
    period: str = ""
    source_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dataset not in DATASETS:
            raise ValueError(f"未知数据集 {self.dataset!r}，可选：{sorted(DATASETS)}")
        if not self.region:
            raise ValueError("region 不能为空（全国口径请用 'CN'）")
        if not self.source:
            raise ValueError("source 不能为空——没有来源就无法回溯核对")

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        """唯一键，与 ``uk_market_point`` 一一对应。

        单独抽出来是为了让「解析结果有没有重复点」能在**入库前**就断言，
        而不是等 MySQL 抛唯一键冲突才发现。
        """
        return (
            self.dataset,
            self.region,
            self.grade,
            self.price_date.isoformat(),
            self.source,
        )

    @property
    def label(self) -> str:
        """人类可读的一行摘要，用于 CLI 输出与日志。"""
        value = "—" if self.value is None else f"{self.value:g}{self.unit}"
        parts = [self.dataset, self.region, self.grade, self.price_date.isoformat(), value]
        if self.change_value is not None:
            parts.append(f"{self.change_value:+g}")
        return " | ".join(p for p in parts if p)
