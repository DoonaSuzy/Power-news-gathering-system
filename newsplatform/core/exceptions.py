"""业务异常体系（改造方案 §4.3 第二层）。

三层错误处理的中间层：路由里语义明确的错误直接 ``raise HTTPException``，
业务语义强的错误走这里的 ``AppError`` 子类，兜底交给
``core/exception_handlers.py``。

约定
----
- ``status_code``：HTTP 状态码，给基础设施（网关/监控）看；
- ``code``：**业务错误码**，给客户端看，稳定且可预测（改造方案 §6.3）；
- ``detail``：人类可读描述，可直接展示给用户。

错误码分段
----------
``0``      成功
``1000+``  请求参数类
``2000+``  认证授权类
``3000+``  资源不存在类
``4000+``  状态冲突类
``4290``   限流
``5000+``  服务端与上游依赖类
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "BadRequest",
    "ValidationFailed",
    "DataOutOfRange",
    "Unauthorized",
    "Forbidden",
    "ResourceNotFound",
    "SourceNotFound",
    "ArticleNotFound",
    "ProvinceNotFound",
    "Conflict",
    "RateLimited",
    "UpstreamUnavailable",
    "InternalError",
    "ERROR_CODES",
    "code_name",
]

#: 业务错误码 → 稳定名称（对外文档与日志共用）
ERROR_CODES: dict[int, str] = {
    0: "ok",
    1000: "bad_request",
    1001: "validation_failed",
    1002: "data_out_of_range",
    2001: "unauthorized",
    2002: "forbidden",
    3001: "resource_not_found",
    3002: "source_not_found",
    3003: "article_not_found",
    3004: "province_not_found",
    4001: "conflict",
    4290: "rate_limited",
    5000: "internal_error",
    5001: "upstream_unavailable",
}


def code_name(code: int) -> str:
    """取错误码的稳定名称。"""
    return ERROR_CODES.get(code, f"unknown_{code}")


class AppError(Exception):
    """所有业务异常的基类。"""

    status_code: int = 400
    code: int = 1000

    def __init__(self, detail: str = "", *, extra: dict[str, Any] | None = None) -> None:
        self.detail = detail or code_name(self.code)
        self.extra = extra or {}
        super().__init__(self.detail)

    @property
    def name(self) -> str:
        return code_name(self.code)

    def to_payload(self) -> dict[str, Any]:
        """转成统一响应体的内容部分。"""
        payload: dict[str, Any] = {"code": self.code, "detail": self.detail}
        payload.update(self.extra)
        return payload


# ----------------------------------------------------------------------
class BadRequest(AppError):
    status_code = 400
    code = 1000


class ValidationFailed(AppError):
    status_code = 422
    code = 1001


class DataOutOfRange(ValidationFailed):
    """入参通过了 Pydantic 校验，但超出了数据库列的可存储范围。

    典型场景：``sources.crawl_interval_hours`` 在 DDL 里是 ``TINYINT``
    （有符号，上限 127），而接口曾一度放行到 168——Pydantic 放行、MySQL 报
    ``1264 Out of range value``，最终以一个裸 500 抛到客户端。

    单独给它一个码，是为了把「请求参数的问题」和「服务端缺陷」区分开：
    前者应当 4xx，后者才是 5xx。
    """

    status_code = 422
    code = 1002


class Unauthorized(AppError):
    status_code = 401
    code = 2001


class Forbidden(AppError):
    status_code = 403
    code = 2002


class ResourceNotFound(AppError):
    status_code = 404
    code = 3001


class SourceNotFound(ResourceNotFound):
    code = 3002


class ArticleNotFound(ResourceNotFound):
    code = 3003


class ProvinceNotFound(ResourceNotFound):
    code = 3004


class Conflict(AppError):
    status_code = 409
    code = 4001


class RateLimited(AppError):
    status_code = 429
    code = 4290


class UpstreamUnavailable(AppError):
    """上游依赖（MySQL / Redis）不可用。"""

    status_code = 503
    code = 5001


class ChannelUnavailable(UpstreamUnavailable):
    """请求了未接入的数据域（如未配置省级库却要 ``channel=province``）。

    单独一个业务码，是为了让客户端能区分「依赖挂了，稍后重试」与
    「这个域根本没接，重试也没用」。
    """

    code = 5002


class InternalError(AppError):
    status_code = 500
    code = 5000
