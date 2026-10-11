"""金融数据爬虫模块。

覆盖简历「实习核心工作内容」第 2 条：Ing 现价、CECI 进口煤指数数据获取、
市场电价日报爬虫模块——附带对应网站的 cookies、token 过期自动获取机制，
以邮件方式传输其登录二维码、验证码，实现告警并可远程恢复其爬虫服务。

模块划分
--------
- :mod:`newsagg.finance.models` —— 三种数据模型（SpotPrice / CoalIndex / MarketPriceDaily）
- :mod:`newsagg.finance.auth` —— AuthProvider 协议 + cookies/token 过期自动获取机制
- :mod:`newsagg.finance.emailer` —— 登录二维码 / 验证码 / 告警的邮件传输（离线 DryRun）
- :mod:`newsagg.finance.alerting` —— 告警记录（5 分钟 SLA）与远程恢复命令下发
- :mod:`newsagg.finance.crawlers` —— 三个爬虫（共享基类、可注入会话与写入器）
- :mod:`newsagg.finance.registry` —— 数据源注册表与爬虫工厂
"""

from __future__ import annotations

from newsagg.finance.alerting import (
    ALERT_SLA_SECONDS,
    Alert,
    AlertManager,
    RecordingRecoveryTransport,
    RecoveryCommand,
    RecoveryHandler,
    RecoveryTransport,
    RemoteRecoveryTransport,
)
from newsagg.finance.auth import (
    AuthProvider,
    CookieTokenManager,
    Credentials,
    FakeAuthProvider,
    HttpSession,
)
from newsagg.finance.crawlers import (
    BaseFinanceCrawler,
    CeciCoalIndexCrawler,
    FinanceCrawlError,
    IngSpotPriceCrawler,
    MarketPriceDailyCrawler,
    MySqlWriter,
    SourceConfig,
    SqliteWriter,
    Writer,
)
from newsagg.finance.emailer import (
    DryRunTransport,
    EmailMessage,
    EmailNotifier,
    RecordingTransport,
    SmtpTransport,
    SmtpTransportImpl,
)
from newsagg.finance.models import CoalIndex, MarketPriceDaily, SpotPrice
from newsagg.finance.registry import (
    FINANCE_SOURCES,
    FinanceSourceMeta,
    available_sources,
    build_all,
    build_crawler,
    get_meta,
)

__all__ = [
    "ALERT_SLA_SECONDS",
    "FINANCE_SOURCES",
    "Alert",
    "AlertManager",
    "AuthProvider",
    "BaseFinanceCrawler",
    "CeciCoalIndexCrawler",
    "CoalIndex",
    "CookieTokenManager",
    "Credentials",
    "DryRunTransport",
    "EmailMessage",
    "EmailNotifier",
    "FakeAuthProvider",
    "FinanceCrawlError",
    "FinanceSourceMeta",
    "HttpSession",
    "IngSpotPriceCrawler",
    "MarketPriceDaily",
    "MarketPriceDailyCrawler",
    "MySqlWriter",
    "RecoveryCommand",
    "RecoveryHandler",
    "RecoveryTransport",
    "RecordingRecoveryTransport",
    "RecordingTransport",
    "RemoteRecoveryTransport",
    "SmtpTransport",
    "SmtpTransportImpl",
    "SourceConfig",
    "SqliteWriter",
    "SpotPrice",
    "Writer",
    "available_sources",
    "build_all",
    "build_crawler",
    "get_meta",
]
