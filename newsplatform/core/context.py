"""请求级上下文（改造方案 §2.2：``flask.g`` → ``contextvars.ContextVar``）。

Flask 用 ``g`` 挂请求级状态，但在异步框架里线程局部变量会串请求；
``contextvars`` 随协程传播，是 asyncio 下唯一正确的选择。

除 ``request_id`` 外，还放 ``client_ip`` / ``api_key_name``：审计日志与限流键
都要用，但都不适合塞进函数签名层层传递。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

__all__ = [
    "RequestContext",
    "get_request_id",
    "new_request_id",
    "set_context",
    "reset_context",
    "current_context",
]


@dataclass(slots=True)
class RequestContext:
    """一次请求的上下文快照。"""

    request_id: str = ""
    client_ip: str = ""
    api_key_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


_CTX: ContextVar[RequestContext | None] = ContextVar("powernews_platform_ctx", default=None)

#: 中文注释：与采集层 logutil 的 X-Request-Id 头保持一致，便于跨系统串联日志
REQUEST_ID_HEADER = "X-Request-Id"


def new_request_id() -> str:
    """生成新的请求 id（32 位十六进制，无连字符，便于日志检索）。"""
    return uuid.uuid4().hex


def get_request_id() -> str:
    """取当前请求 id；不在请求上下文时返回空串。"""
    ctx = _CTX.get()
    return ctx.request_id if ctx else ""


def set_context(ctx: RequestContext) -> Token:
    """绑定请求上下文，返回可用于还原的 token。"""
    return _CTX.set(ctx)


def reset_context(token: Token) -> None:
    """还原请求上下文。"""
    _CTX.reset(token)


def current_context() -> RequestContext:
    """取当前上下文（不存在时返回空对象，避免到处判 None）。"""
    return _CTX.get() or RequestContext()
