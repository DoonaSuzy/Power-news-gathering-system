"""限流（改造方案 §6.1 / §7：保护采集库不被读爆）。

算法：**Redis 固定窗口计数**（``INCR`` + 首次 ``EXPIRE``）。
够简单、够快、无额外状态；对本项目「限制单个调用方每分钟请求数」的场景完全够用
（滑动窗口/令牌桶的额外精度在这里换不来实际收益）。

降级策略（关键）
    Redis 不可用时**放行**而不是拒绝。理由：限流是保护措施，不是业务正确性前提；
    让「缓存挂了」升级成「全站 503」是典型的过度耦合。降级事件会打 WARNING 日志，
    可被监控捕获。

键设计
    ``powernews:platform:rl:<身份>:<窗口起始秒>``
    身份优先取 API Key 名（比 IP 稳定，且一个 Key 是一份配额），无 Key 时退化为 IP。
"""

from __future__ import annotations

import time
from typing import Any

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.context import current_context
from newsplatform.core.exceptions import RateLimited
from newsplatform.core.logging import get_logger

log = get_logger("ratelimit")

__all__ = ["RateLimiter", "get_rate_limiter", "enforce_rate_limit", "reset_rate_limiter"]

_KEY_PREFIX = "powernews:platform:rl"


class RateLimiter:
    """固定窗口限流器。"""

    def __init__(self, settings: PlatformSettings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_platform_settings()
        self._client = client

    @property
    def client(self) -> Any | None:
        """惰性建立 Redis 客户端；不可用时返回 None（触发降级放行）。"""
        if self._client is None:
            try:
                import redis

                # protocol=2：RESP2 兼容所有 Redis 版本（详见 newsagg/queue.py 注释）
                self._client = redis.Redis.from_url(
                    self.settings.redis_url, decode_responses=True, protocol=2
                )
                self._client.ping()
            except Exception as exc:  # noqa: BLE001
                log.warning("Redis 不可用，限流降级为放行: %s", exc)
                self._client = _NullClient()
        return self._client

    def _identity(self, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        ctx = current_context()
        return ctx.api_key_name or ctx.client_ip or "anonymous"

    def check(self, *, limit: int | None = None, identity: str | None = None) -> tuple[bool, int]:
        """消耗一次配额。返回 ``(是否放行, 剩余额度)``。"""
        limit = int(limit or self.settings.rate_limit_per_minute or 0)
        if limit <= 0:
            return True, -1

        window = int(time.time() // 60)
        key = f"{_KEY_PREFIX}:{self._identity(identity)}:{window}"
        client = self.client
        if client is None or isinstance(client, _NullClient):
            return True, -1

        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 120)  # 留一倍余量，避免边界抖动
            used = int(pipe.execute()[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("限流计数失败，放行: %s", exc)
            return True, -1

        return (used <= limit), max(0, limit - used)

    def enforce(self, *, limit: int | None = None, identity: str | None = None) -> int:
        """超限抛 :class:`RateLimited`，否则返回剩余额度。"""
        allowed, remaining = self.check(limit=limit, identity=identity)
        if not allowed:
            raise RateLimited(
                f"请求过于频繁，上限 {limit or self.settings.rate_limit_per_minute} 次/分钟",
                extra={"retry_after_seconds": 60},
            )
        return remaining


class _NullClient:
    """Redis 不可用时的占位客户端（所有操作均为 no-op）。"""

    def pipeline(self):  # pragma: no cover - 仅降级路径
        raise RuntimeError("redis unavailable")

    def ping(self) -> bool:  # pragma: no cover
        return False


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """取全局限流器。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    """重置（测试用）。"""
    global _limiter
    _limiter = None


async def enforce_rate_limit() -> int:
    """FastAPI 依赖：按当前身份限流。"""
    return get_rate_limiter().enforce()
