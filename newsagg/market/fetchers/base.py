"""市场行情采集器基类与 HTTP 客户端封装。

设计要点与 newsagg 既有部分保持一致：**抓取与入库分离**。
采集器只产出 :class:`~newsagg.market.models.MarketPoint`，不碰数据库——
这样解析逻辑可以直接喂一段写死的 HTML 做单测，不需要起库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from newsagg.logutil import get_logger
from newsagg.market.models import MarketPoint

__all__ = ["MarketFetcher", "build_client"]

log = get_logger("market")

#: 统一 UA。**带项目标识而不是伪装成浏览器**：对方站点的运维能识别来源，
#: 出问题时能找到人；伪装成 Chrome 反而在法律与礼貌上都站不住脚。
DEFAULT_UA = (
    "powernews-market/0.1 (research aggregator; "
    "+https://github.com/DoonaSuzy/Power-news-gathering-system)"
)

#: 默认超时（秒）。行情页都不大，30 秒足够；卡住说明对方在限流，早点失败更好。
DEFAULT_TIMEOUT = 30.0


def build_client(
    *,
    verify: bool = True,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """构造采集用 HTTP 客户端。

    Args:
        verify: 是否校验 TLS 证书。默认 ``True``；只有对方站点证书链确实不完整时
            才显式关掉，并在对应采集器里写明原因（见 ``power_gzpec``）。
        headers: 额外请求头，会覆盖默认值。用于个别站点的 ``Referer`` 校验
            （见 ``ceci_cec``）——这类站点对**接口**请求也查来源页。

    Returns:
        配好超时、UA、语言与重定向跟随的同步客户端。
    """
    merged = {
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if headers:
        merged.update(headers)
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        verify=verify,
        headers=merged,
    )


class MarketFetcher(ABC):
    """采集器基类。

    子类必须声明 :attr:`dataset` 与 :attr:`source`，并实现 :meth:`fetch`。
    """

    #: 数据集标识，取值见 :data:`newsagg.market.models.DATASETS`。
    dataset: str = ""
    #: 来源标识，写入 ``market_prices.source``。
    source: str = ""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _make_client(self) -> httpx.Client:
        """构造默认客户端。子类需要额外请求头（如 ``Referer``）时覆写这里。"""
        return build_client()

    @property
    def client(self) -> httpx.Client:
        """懒加载的 HTTP 客户端（复用连接，避免每页都重开 TLS）。"""
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def close(self) -> None:
        """释放连接。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    @abstractmethod
    def fetch(self) -> list[MarketPoint]:
        """抓取并解析，返回数据点列表。实现里不应吞掉异常。"""

    def __enter__(self) -> MarketFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
