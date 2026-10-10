"""全局异常处理器（改造方案 §4.3 第三层）。

注册顺序很重要——FastAPI 按「最具体的异常类」优先匹配，所以：

1. ``AppError``        → 业务异常，带稳定业务码
2. ``HTTPException``   → 路由里手动抛的 HTTP 语义错误
3. ``RequestValidationError`` → Pydantic 参数校验失败（422）
4. ``Exception``       → 兜住一切未预期异常，**不回显内部细节**

第 4 条是安全边界：生产环境把堆栈/表名/SQL 回给客户端等于免费送攻击面。
只回 ``"服务器内部错误"`` + ``request_id``，细节写日志（带完整堆栈），
排障靠 request_id 对齐。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from newsplatform.core.config import get_platform_settings
from newsplatform.core.exceptions import AppError, code_name
from newsplatform.core.logging import get_logger
from newsplatform.core.response import fail

log = get_logger("errors")

__all__ = ["register_exception_handlers"]


def register_exception_handlers(app: FastAPI) -> None:
    """把三层处理器挂到应用上。"""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # 4xx 是预期内的（客户端问题），INFO/WARNING；5xx 是服务端问题，ERROR
        log_fn = log.error if exc.status_code >= 500 else log.warning
        log_fn(
            "业务异常 %s(%s): %s %s -> %s",
            exc.name, exc.code, request.method, request.url.path, exc.detail,
        )
        return JSONResponse(status_code=exc.status_code, content=fail(exc.code, exc.detail))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = 3001 if exc.status_code == 404 else 1000
        if exc.status_code == 401:
            code = 2001
        elif exc.status_code == 403:
            code = 2002
        elif exc.status_code == 429:
            code = 4290
        detail = exc.detail if isinstance(exc.detail, str) else code_name(code)
        log.warning("HTTP %s: %s %s -> %s", exc.status_code, request.method,
                    request.url.path, detail)
        response = JSONResponse(status_code=exc.status_code, content=fail(code, detail))
        # 保留 WWW-Authenticate 等标准头
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", [])[1:]) or "body",
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        log.warning("参数校验失败: %s %s -> %s", request.method, request.url.path, errors)
        return JSONResponse(
            status_code=422,
            content=fail(1001, "请求参数校验失败", extra={"errors": errors}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未捕获异常: %s %s", request.method, request.url.path)
        settings = get_platform_settings()
        # debug 模式下回显异常类型，方便本地定位；生产只给通用文案
        detail = f"{type(exc).__name__}: {exc}" if settings.debug else "服务器内部错误"
        return JSONResponse(status_code=500, content=fail(5000, detail))
