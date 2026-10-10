"""中台核心设施：配置、数据库、异常、日志、中间件、鉴权、限流、依赖注入。

本包**不 import** ``newsplatform.services`` / ``newsplatform.api``，
以保证依赖方向单向（core ← services ← api → main）。
``security`` 会反向依赖 ``repositories``（读 API Key），因此这里不把它
放进 ``__all__`` 的立即导入里——需要它的地方直接
``from newsplatform.core.security import require_auth`` 即可。
"""

from __future__ import annotations

from newsplatform.core.config import (
    PlatformSettings,
    get_platform_settings,
    reset_platform_settings,
)
from newsplatform.core.context import RequestContext, get_request_id, new_request_id
from newsplatform.core.database import (
    AsyncDatabase,
    dispose_database,
    get_collector_session,
    get_database,
    get_platform_session,
)
from newsplatform.core.exceptions import (
    AppError,
    ArticleNotFound,
    BadRequest,
    Conflict,
    Forbidden,
    InternalError,
    RateLimited,
    ResourceNotFound,
    SourceNotFound,
    Unauthorized,
    UpstreamUnavailable,
    ValidationFailed,
)
from newsplatform.core.logging import get_logger, setup_platform_logging
from newsplatform.core.response import Envelope, PageData, fail, ok

__all__ = [
    # config
    "PlatformSettings",
    "get_platform_settings",
    "reset_platform_settings",
    # context
    "RequestContext",
    "get_request_id",
    "new_request_id",
    # database
    "AsyncDatabase",
    "get_database",
    "get_platform_session",
    "get_collector_session",
    "dispose_database",
    # exceptions
    "AppError",
    "BadRequest",
    "ValidationFailed",
    "Unauthorized",
    "Forbidden",
    "ResourceNotFound",
    "SourceNotFound",
    "ArticleNotFound",
    "Conflict",
    "RateLimited",
    "UpstreamUnavailable",
    "InternalError",
    # logging
    "get_logger",
    "setup_platform_logging",
    # response
    "Envelope",
    "PageData",
    "ok",
    "fail",
]
