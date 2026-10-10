"""采集层异常体系（改造方案 §4.3「自定义异常体系」）。

统一基类 :class:`PowerNewsError`，所有业务异常携带稳定的 ``code``，
便于日志聚合与告警规则匹配。
"""

from __future__ import annotations

__all__ = [
    "PowerNewsError",
    "ConfigError",
    "SourceNotFound",
    "JobNotFound",
    "LeaseBusy",
    "LeaseLost",
    "AdapterError",
    "AdapterNotFound",
    "ParseError",
    "CrawlRateLimited",
    "CrawlBlocked",
    "BudgetExceeded",
    "QueueError",
]


class PowerNewsError(Exception):
    """采集层所有异常的基类。"""

    code = "powernews_error"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or self.code)


class ConfigError(PowerNewsError):
    """配置缺失或非法（如未设置数据库密码）。"""

    code = "config_error"


class SourceNotFound(PowerNewsError):
    """来源不存在或未启用。"""

    code = "source_not_found"


class JobNotFound(PowerNewsError):
    """采集任务不存在。"""

    code = "job_not_found"


class LeaseBusy(PowerNewsError):
    """任务已被其他 worker 持有租约。"""

    code = "lease_busy"


class LeaseLost(PowerNewsError):
    """租约在运行期失效（需立即停止产出）。"""

    code = "lease_lost"


class AdapterError(PowerNewsError):
    """适配器通用错误。"""

    code = "adapter_error"


class AdapterNotFound(AdapterError):
    """未注册的适配器。"""

    code = "adapter_not_found"


class ParseError(AdapterError):
    """列表页 / 详情页解析失败。"""

    code = "parse_error"


class CrawlRateLimited(PowerNewsError):
    """被目标站点限流（HTTP 429）。"""

    code = "crawl_rate_limited"


class CrawlBlocked(PowerNewsError):
    """被目标站点封禁（HTTP 403/405/412），触发停机冷却。"""

    code = "crawl_blocked"


class BudgetExceeded(PowerNewsError):
    """单任务超出请求预算或时间预算。"""

    code = "budget_exceeded"


class QueueError(PowerNewsError):
    """Redis 队列异常。"""

    code = "queue_error"
