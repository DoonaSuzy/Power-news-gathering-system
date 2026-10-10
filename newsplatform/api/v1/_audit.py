"""审计辅助（改造方案 §6.3：所有写操作记录审计日志）。

用上下文管理器而不是装饰器，是因为审计要拿到「最终状态码」——
写操作抛异常时也得留痕（谁在什么时候试图改什么、失败了），
装饰器拿不到异常里的状态码，``try/finally`` 可以。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.context import get_request_id
from newsplatform.core.exceptions import AppError
from newsplatform.core.middleware import client_ip_of
from newsplatform.core.security import AuthContext
from newsplatform.services.admin_service import AuditService

__all__ = ["audit_write"]


@asynccontextmanager
async def audit_write(
    session: AsyncSession,
    request: Request,
    auth: AuthContext | None,
    *,
    detail: str = "",
) -> AsyncIterator[None]:
    """记录一次写操作。

    :param session: **平台库**会话（审计表在中台自有库里）
    :param auth: 认证上下文，可为 None（内部调用）
    """
    started = time.perf_counter()
    status_code = 200
    try:
        yield
    except AppError as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        await AuditService(session).record(
            request_id=get_request_id(),
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            api_key_id=auth.key_id if auth else None,
            api_key_name=auth.name if auth else "",
            client_ip=client_ip_of(request),
            user_agent=request.headers.get("user-agent", ""),
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
        )
