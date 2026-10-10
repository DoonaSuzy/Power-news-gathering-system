"""请求中间件（改造方案 §2.2：``before_request`` → ASGI 中间件）。

一个中间件干三件事，顺序不能颠倒：

1. **生成/透传 request_id**：外部带了 ``X-Request-Id`` 就用它（便于跨系统串联），
   否则新生成一个；同时写入 ``contextvars`` 与响应头。
2. **计时**：记录 ``duration_ms``。
3. **写访问日志**：方法、路径、状态码、耗时、客户端 IP、request_id 一条打齐。
   4xx 用 WARNING、5xx 用 ERROR，符合改造方案 §4.3 的「错误日志规范」。

为什么不用 ``@app.middleware("http")``？
    那个装饰器等价于 ``BaseHTTPMiddleware``，但显式继承更容易在测试里单独实例化，
    也更容易看清执行顺序（``dispatch`` 里 ``call_next`` 前后即「前/后处理」）。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from newsplatform.core.context import RequestContext, REQUEST_ID_HEADER, reset_context, set_context
from newsplatform.core.logging import bind_log_context, clear_log_context, get_logger

log = get_logger("access")

__all__ = ["RequestContextMiddleware", "client_ip_of"]


def client_ip_of(request: Request) -> str:
    """取客户端 IP：优先反代头，其次 socket。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求 id + 日志上下文 + 访问日志。"""

    def __init__(self, app: ASGIApp, *, log_health: bool = False) -> None:
        super().__init__(app)
        self.log_health = log_health

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or ""
        from newsplatform.core.context import new_request_id

        request_id = request_id.strip() or new_request_id()

        ctx = RequestContext(request_id=request_id, client_ip=client_ip_of(request))
        token = set_context(ctx)
        bind_log_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=ctx.client_ip,
        )

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            skip = not self.log_health and request.url.path.endswith("/health")
            if not skip:
                log_fn = (
                    log.error if status_code >= 500
                    else log.warning if status_code >= 400
                    else log.info
                )
                log_fn(
                    "%s %s -> %s (%.2fms)",
                    request.method, request.url.path, status_code, duration_ms,
                )
            clear_log_context()
            reset_context(token)
