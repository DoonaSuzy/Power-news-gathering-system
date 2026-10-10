"""轻量 Redis 缓存（改造方案 §6.1「增加缓存层」）。

只做两件事：``get_json`` / ``set_json``。
**Redis 不可用时一律视为 miss**，绝不因为缓存故障让请求失败——
缓存是加速手段，不是正确性依赖。

键前缀 ``powernews:platform:cache:``，与采集层的布隆位图/队列键隔离，
运维清缓存时不会误伤任务队列。
"""

from __future__ import annotations

import json
from typing import Any

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.logging import get_logger

log = get_logger("cache")

__all__ = ["Cache", "get_cache", "reset_cache"]

_KEY_PREFIX = "powernews:platform:cache"


class Cache:
    """JSON 缓存。"""

    def __init__(self, settings: PlatformSettings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_platform_settings()
        self._client = client
        self._disabled = False

    @property
    def client(self) -> Any | None:
        if self._disabled:
            return None
        if self._client is None:
            try:
                import redis

                # protocol=2 兼容所有 Redis 版本
                self._client = redis.Redis.from_url(
                    self.settings.redis_url, decode_responses=True, protocol=2
                )
                self._client.ping()
            except Exception as exc:  # noqa: BLE001
                log.warning("Redis 不可用，缓存已禁用: %s", exc)
                self._disabled = True
                return None
        return self._client

    def get_json(self, key: str) -> Any | None:
        """读缓存；miss 或异常返回 None。"""
        client = self.client
        if client is None:
            return None
        try:
            raw = client.get(f"{_KEY_PREFIX}:{key}")
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.warning("读缓存失败 %s: %s", key, exc)
            return None

    def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """写缓存；失败静默（只记日志）。"""
        client = self.client
        if client is None:
            return
        try:
            client.set(
                f"{_KEY_PREFIX}:{key}",
                json.dumps(value, ensure_ascii=False, default=str),
                ex=int(ttl or self.settings.cache_ttl_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("写缓存失败 %s: %s", key, exc)

    def delete(self, *keys: str) -> int:
        """删除若干键。"""
        client = self.client
        if client is None or not keys:
            return 0
        try:
            return int(client.delete(*[f"{_KEY_PREFIX}:{k}" for k in keys]) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def ping(self) -> bool:
        """连通性。"""
        client = self.client
        if client is None:
            return False
        try:
            return bool(client.ping())
        except Exception:  # noqa: BLE001
            return False


_cache: Cache | None = None


def get_cache() -> Cache:
    """取全局缓存实例。"""
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache


def reset_cache() -> None:
    """重置（测试用）。"""
    global _cache
    _cache = None
