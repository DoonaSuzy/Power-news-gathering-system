"""统一响应体（改造方案 §6.3「所有API返回统一响应体」）。

成功：``{"code": 0, "data": {...}, "request_id": "..."}``
失败：``{"code": 3003, "detail": "...", "request_id": "..."}``

``request_id`` 一律从 ``contextvars`` 取——由 ``core/middleware.py`` 的
请求中间件写入，因此路由函数**不需要**手工把它传来传去。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from newsplatform.core.context import get_request_id

__all__ = ["Envelope", "PageData", "ok", "fail", "json_ok", "json_fail"]

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """统一响应体的 Pydantic 形态（仅用于 OpenAPI 文档展示）。"""

    code: int = Field(0, description="业务码，0 表示成功")
    data: T | None = Field(None, description="业务数据")
    request_id: str = Field("", description="请求追踪 id，与响应头 X-Request-Id 一致")


class PageData(BaseModel, Generic[T]):
    """分页数据的标准结构。"""

    items: list[T] = Field(default_factory=list)
    total: int = Field(0, description="命中总条数")
    page: int = Field(1, description="当前页码，从 1 开始")
    page_size: int = Field(20, description="每页条数")
    pages: int = Field(0, description="总页数")


def ok(data: Any = None, *, request_id: str | None = None) -> dict[str, Any]:
    """构造成功响应体。"""
    return {
        "code": 0,
        "data": data,
        "request_id": request_id if request_id is not None else get_request_id(),
    }


def fail(code: int, detail: str, *, request_id: str | None = None,
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造失败响应体。"""
    payload: dict[str, Any] = {
        "code": code,
        "detail": detail,
        "request_id": request_id if request_id is not None else get_request_id(),
    }
    if extra:
        payload.update(extra)
    return payload


def json_ok(data: Any = None, *, status_code: int = 200) -> JSONResponse:
    """直接返回 JSONResponse 的成功响应（需要自定义状态码时用）。"""
    return JSONResponse(status_code=status_code, content=ok(data))


def json_fail(code: int, detail: str, *, status_code: int = 400,
              extra: dict[str, Any] | None = None) -> JSONResponse:
    """直接返回 JSONResponse 的失败响应。"""
    return JSONResponse(status_code=status_code, content=fail(code, detail, extra=extra))
