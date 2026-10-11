"""金融数据爬虫注册表：三个来源的元数据与工厂函数。

``FINANCE_SOURCES`` 以数据源名为键登记 Ing 现价、CECI 进口煤指数、市场电价
日报三类爬虫，附带类别与采集间隔，供调度器按 ``interval_seconds`` 轮询。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newsagg.finance.alerting import AlertManager
from newsagg.finance.auth import AuthProvider, CookieTokenManager, HttpSession
from newsagg.finance.crawlers import (
    BaseFinanceCrawler,
    CeciCoalIndexCrawler,
    IngSpotPriceCrawler,
    MarketPriceDailyCrawler,
    SourceConfig,
    Writer,
)
from newsagg.finance.emailer import EmailNotifier


@dataclass(frozen=True)
class FinanceSourceMeta:
    """注册表中的一条金融数据源元数据。"""

    name: str
    category: str
    interval_seconds: int
    crawler_cls: type[BaseFinanceCrawler]
    base_url: str
    table: str

    @property
    def config(self) -> SourceConfig:
        """底层 SourceConfig（爬虫类上挂载的配置）。"""
        return self.crawler_cls.SOURCE


FINANCE_SOURCES: dict[str, FinanceSourceMeta] = {
    meta.name: meta
    for meta in (
        FinanceSourceMeta(
            name="ing_spot_price",
            category="有色金属现价",
            interval_seconds=3600,
            crawler_cls=IngSpotPriceCrawler,
            base_url=IngSpotPriceCrawler.SOURCE.base_url,
            table=IngSpotPriceCrawler.SOURCE.table,
        ),
        FinanceSourceMeta(
            name="ceci_coal_index",
            category="进口煤指数",
            interval_seconds=86400,
            crawler_cls=CeciCoalIndexCrawler,
            base_url=CeciCoalIndexCrawler.SOURCE.base_url,
            table=CeciCoalIndexCrawler.SOURCE.table,
        ),
        FinanceSourceMeta(
            name="market_price_daily",
            category="市场电价日报",
            interval_seconds=86400,
            crawler_cls=MarketPriceDailyCrawler,
            base_url=MarketPriceDailyCrawler.SOURCE.base_url,
            table=MarketPriceDailyCrawler.SOURCE.table,
        ),
    )
}


def available_sources() -> tuple[str, ...]:
    """全部已注册的金融数据源名称。"""
    return tuple(FINANCE_SOURCES)


def get_meta(name: str) -> FinanceSourceMeta | None:
    """按名称取元数据，不存在返回 None。"""
    return FINANCE_SOURCES.get(name)


def build_crawler(
    name: str,
    *,
    session: HttpSession,
    provider: AuthProvider,
    notifier: EmailNotifier | None = None,
    writer: Writer | None = None,
    alert_manager: AlertManager | None = None,
    auth: CookieTokenManager | None = None,
) -> BaseFinanceCrawler:
    """按名称构造爬虫实例（依赖全部注入，可离线运行）。

    ``auth`` 已提供时直接复用（例如多个爬虫共享同一登录态管理器），
    否则基于 ``provider`` + ``session`` 新建。
    """
    meta = FINANCE_SOURCES.get(name)
    if meta is None:
        raise KeyError(f"未注册的金融数据源: {name}")
    manager = auth or CookieTokenManager(provider, session=session, source=name)
    return meta.crawler_cls(
        session=session,
        auth=manager,
        notifier=notifier,
        writer=writer,
        alert_manager=alert_manager,
    )


def build_all(
    *,
    session: HttpSession,
    provider: AuthProvider,
    notifier: EmailNotifier | None = None,
    writer: Any = None,
    alert_manager: AlertManager | None = None,
) -> dict[str, BaseFinanceCrawler]:
    """构造全部注册爬虫（默认共享同一 CookieTokenManager 登录态）。"""
    manager = CookieTokenManager(provider, session=session, source="finance")
    return {
        name: build_crawler(
            name,
            session=session,
            provider=provider,
            notifier=notifier,
            writer=writer,
            alert_manager=alert_manager,
            auth=manager,
        )
        for name in FINANCE_SOURCES
    }


__all__ = [
    "FINANCE_SOURCES",
    "FinanceSourceMeta",
    "available_sources",
    "build_all",
    "build_crawler",
    "get_meta",
]
