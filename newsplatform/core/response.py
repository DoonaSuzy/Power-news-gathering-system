"""统一响应体（改造方案 §6.3「所有API返回统一响应体」）。

成功：``{"code": 0, "message": "ok", "data": {...}, "request_id": "...", "timestamp": "..."}``
失败：``{"code": 3003, "message": "...", "detail": "...", "request_id": "...", "timestamp": "..."}``

``request_id`` 一律从 ``contextvars`` 取——由 ``core/middleware.py`` 的
请求中间件写入，因此路由函数**不需要**手工把它传来传去。

关于 ``message`` / ``timestamp``（合并省级后端时新增）
------------------------------------------------------
省级中台的信封是 ``{code, message, data, request_id, timestamp}``，电力侧少了
``message`` 与 ``timestamp`` 两项。合并后对外只有一个入口，两种信封并存会让
客户端写两套解析。**取并集是纯加法**：老客户端忽略多出来的字段即可，
不需要改一行代码。

失败体里 ``detail`` 保留为 ``message`` 的别名一个版本：电力侧现有调用方
（含 SDK 与前端）读的是 ``detail``，直接改名等于破坏性变更。下个版本再收掉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from newsplatform.core.context import get_request_id

__all__ = ["Envelope", "PageData", "ok", "fail", "json_ok", "json_fail"]

T = TypeVar("T")


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（与省级信封同格式，带 ``+00:00``）。"""
    return datetime.now(UTC).isoformat()


class Envelope(BaseModel, Generic[T]):
    """统一响应体的 Pydantic 形态（仅用于 OpenAPI 文档展示）。"""

    code: int = Field(0, description="业务码，0 表示成功")
    message: str = Field("ok", description="人可读的说明；成功固定 ok")
    data: T | None = Field(None, description="业务数据")
    request_id: str = Field("", description="请求追踪 id，与响应头 X-Request-Id 一致")
    timestamp: str = Field("", description="服务端 UTC 时间（ISO-8601）")


class PageData(BaseModel, Generic[T]):
    """分页数据的标准结构。"""

    items: list[T] = Field(default_factory=list)
    total: int = Field(0, description="命中总条数")
    page: int = Field(1, description="当前页码，从 1 开始")
    page_size: int = Field(20, description="每页条数")
    pages: int = Field(0, description="总页数")


def ok(data: Any = None, *, request_id: str | None = None,
       message: str = "ok") -> dict[str, Any]:
    """构造成功响应体。"""
    return {
        "code": 0,
        "message": message,
        "data": data,
        "request_id": request_id if request_id is not None else get_request_id(),
        "timestamp": _now_iso(),
    }


def fail(code: int, detail: str, *, request_id: str | None = None,
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造失败响应体。

    ``message`` 是新字段，``detail`` 保留为同义别名（老客户端读的是它）。
    """
    payload: dict[str, Any] = {
        "code": code,
        "message": detail,
        "detail": detail,
        "request_id": request_id if request_id is not None else get_request_id(),
        "timestamp": _now_iso(),
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
