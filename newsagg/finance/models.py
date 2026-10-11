"""金融数据模型：Ing 现价、CECI 进口煤指数、市场电价日报的行结构。

全部为小型、强类型 dataclass；``ts`` 统一使用 ISO-8601 文本（爬虫自站点
原文提取，便于落库与回溯），解析逻辑集中在 :mod:`newsagg.finance.crawlers`。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SpotPrice:
    """Ing 现价（有色金属现货价格行情）。"""

    index_name: str
    price: float
    unit: str
    ts: str

    def to_row(self) -> dict[str, object]:
        """转为可写入存储的 dict 行。"""
        return asdict(self)


@dataclass(frozen=True)
class CoalIndex:
    """CECI 进口煤指数（中国电力企业联合会进口煤价格指数）。"""

    name: str
    value: float
    ts: str
    change: float = 0.0

    def to_row(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MarketPriceDaily:
    """省级电力市场电价日报（按地区分列的交易电价）。"""

    region: str
    price: float
    ts: str

    def to_row(self) -> dict[str, object]:
        return asdict(self)


__all__ = ["SpotPrice", "CoalIndex", "MarketPriceDaily"]
