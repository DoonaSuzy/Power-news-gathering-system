"""中台日志配置（改造方案 §4.4「日志记录体系」）。

与采集层 ``newsagg/logutil.py`` 的差异：
- 采集层关心 ``job_id`` / ``run_id``（任务是长生命周期的）；
- 中台关心 ``request_id`` / ``path`` / ``status`` / ``duration_ms``（请求是短生命周期的）。

相同点：都用 ``contextvars`` 注入上下文字段，都在 ``POWERNEWS_*_LOG_JSON=1`` 时
切成单行 JSON，便于 Loki / ELK 采集。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

__all__ = ["setup_platform_logging", "get_logger", "bind_log_context", "LogContextFilter"]

#: 额外的结构化字段（写入 JSON 日志）
#
# default 用 None 而不是 {}：可变对象当默认值时，所有 context 共享同一个 dict，
# 一旦有人执行 _CONTEXT.get()['k'] = v 就地修改，就会跨请求串味。
# 读的地方统一写 ``_CONTEXT.get() or {}``。
_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "powernews_platform_log_ctx", default=None
)

_EXTRA_KEYS = ("request_id", "method", "path", "status_code", "duration_ms", "client_ip")

_LOGGER_NAME = "powernews.platform"
_configured = False


class LogContextFilter(logging.Filter):
    """把 contextvars 里的字段挂到 LogRecord 上，供格式化器取用。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API 约定
        ctx = _CONTEXT.get() or {}
        if ctx:
            record._ctx = ctx  # type: ignore[attr-defined]
        return True


class _JsonFormatter(logging.Formatter):
    """单行 JSON，字段固定便于聚合查询。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "_ctx", None)
        if ctx:
            payload.update(ctx)
        for key in _EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """人类可读格式，带上上下文里的 request_id 便于肉眼对齐。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "_ctx", None) or {}
        rid = ctx.get("request_id")
        return f"{base} [rid={rid}]" if rid else base


def bind_log_context(**kwargs: Any) -> None:
    """更新日志上下文（合并语义，不覆盖未传入的键）。"""
    ctx = dict(_CONTEXT.get() or {})
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    _CONTEXT.set(ctx)


def clear_log_context() -> None:
    """清空日志上下文（请求结束时调用）。"""
    _CONTEXT.set({})


def setup_platform_logging(level: str | None = None, *, force: bool = False) -> None:
    """初始化中台日志（幂等）。"""
    global _configured
    if _configured and not force:
        return

    level_name = (level or os.environ.get("POWERNEWS_PLATFORM_LOG_LEVEL") or "INFO").upper()
    use_json = os.environ.get("POWERNEWS_PLATFORM_LOG_JSON", "").strip() in {"1", "true", "yes"}

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(LogContextFilter())
    handler.setFormatter(
        _JsonFormatter()
        if use_json
        else _TextFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)

    # uvicorn 的访问日志与我们的访问中间件重复，降噪到 WARNING
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    """取中台日志器（首次调用自动初始化）。"""
    if not _configured:
        setup_platform_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
