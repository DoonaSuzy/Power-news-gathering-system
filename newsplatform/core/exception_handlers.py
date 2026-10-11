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
from sqlalchemy.exc import DataError, IntegrityError
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

    @app.exception_handler(DataError)
    async def _data_error(request: Request, exc: DataError) -> JSONResponse:
        """Pydantic 放行、但数据库列装不下的入参 → 422 而不是 500。

        这类错误「看起来像服务端故障」（SQLAlchemy 抛 DataError、日志里是完整
        堆栈），本质却是**客户端给了非法值**，只是校验边界没对齐 DDL 的列类型。
        例如 ``sources.crawl_interval_hours`` 是 ``TINYINT``（上限 127），
        接口曾放行到 168，MySQL 便报 ``1264 Out of range value``。

        单独处理它是为了：
        1. 客户端拿到 **4xx**（可自助修正），而不是会触发重试/告警的 5xx；
        2. 错误码稳定为 1002，便于前端区分「参数越界」与「服务端异常」。

        对外文案不带上库名/列名，细节只进日志。
        """
        log.exception("数据越界: %s %s", request.method, request.url.path)
        settings = get_platform_settings()
        detail = "数据超出字段允许范围" if not settings.debug else f"{type(exc).__name__}: {exc}"
        return JSONResponse(status_code=422, content=fail(1002, detail))

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        """唯一键 / 外键冲突 → 409，而不是 500。"""
        log.exception("完整性约束冲突: %s %s", request.method, request.url.path)
        settings = get_platform_settings()
        detail = "数据约束冲突（唯一键或外键）" if not settings.debug else f"{type(exc).__name__}: {exc}"
        return JSONResponse(status_code=409, content=fail(4001, detail))

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
