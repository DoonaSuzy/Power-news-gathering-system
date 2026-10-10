"""日志配置（改造方案 §4.4「日志记录体系」）。

设计要点：

- **结构化**：默认控制台输出人类可读格式；设置 ``POWERNEWS_LOG_JSON=1``
  可切换为 JSON 行格式，便于 ELK / Loki 采集。
- **请求级 / 任务级追踪**：:func:`bind_context` 基于 ``contextvars`` 注入
  ``job_id`` / ``run_id`` / ``request_id``，异步调用链中自动携带。
- **分层记录**：采集层用 :func:`get_logger`，Scrapy 自身日志由
  ``crawl/settings.py`` 降噪到 WARNING。
- **文件轮转**：设置 ``POWERNEWS_LOG_DIR`` 后按天轮转，保留 ``LOG_BACKUP_DAYS`` 天。
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "get_logger",
    "bind_context",
    "current_context",
    "setup_logging",
    "JsonFormatter",
]

#: 异步安全的日志上下文（contextvars 随协程/线程传播）
_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "powernews_log_context", default={}
)

_EXTRA_KEYS = ("job_id", "run_id", "request_id", "source", "adapter", "province_code")


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式化器。"""

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


class _ContextFilter(logging.Filter):
    """把 contextvars 中的上下文字段挂到每条日志记录上。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _LOG_CONTEXT.get()
        if ctx:
            record._ctx = ctx  # type: ignore[attr-defined]
        return True


def bind_context(**kwargs: Any) -> contextvars.Token:
    """绑定日志上下文，返回可用于还原的 token。"""
    ctx = dict(_LOG_CONTEXT.get())
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    return _LOG_CONTEXT.set(ctx)


def current_context() -> dict[str, Any]:
    """当前日志上下文副本。"""
    return dict(_LOG_CONTEXT.get())


_configured = False


def setup_logging(
    level: str | None = None,
    log_dir: str | Path | None = None,
    *,
    backup_days: int = 14,
) -> None:
    """初始化根日志器（幂等，可重复调用）。"""
    global _configured
    if _configured:
        return

    level_name = (level or os.environ.get("POWERNEWS_LOG_LEVEL") or "INFO").upper()
    use_json = os.environ.get("POWERNEWS_LOG_JSON", "").strip() in {"1", "true", "yes"}
    directory = log_dir or os.environ.get("POWERNEWS_LOG_DIR") or ""

    root = logging.getLogger("powernews")
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.propagate = False
    root.handlers.clear()

    console = logging.StreamHandler(stream=sys.stdout)
    console.addFilter(_ContextFilter())
    if use_json:
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(console)

    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            path / "powernews.log",
            when="midnight",
            backupCount=backup_days,
            encoding="utf-8",
        )
        file_handler.addFilter(_ContextFilter())
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str = "powernews") -> logging.Logger:
    """取业务日志器（首次调用自动初始化）。"""
    if not _configured:
        setup_logging()
    if name == "powernews" or name.startswith("powernews."):
        return logging.getLogger(name)
    return logging.getLogger(f"powernews.{name}")
