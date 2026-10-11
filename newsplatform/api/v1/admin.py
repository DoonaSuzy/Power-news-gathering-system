"""中台管理接口（``/api/v1/admin`` 系列）：API Key 与审计日志。

鉴权
----
全部端点要求 ``admin`` 权限（``require_admin``）。

引导机制
--------
库内**一条 Key 都没有**时，可用 ``POWERNEWS_PLATFORM_BOOTSTRAP_ADMIN_TOKEN``
访问，便于首次调用 ``POST /admin/api-keys`` 创建正式 Key。
一旦库里存在任意一条 Key，引导令牌**立即失效**（见 ``core/security.py``），
避免留下永久后门。

安全约定
--------
- 明文 Key **只在创建响应里返回这一次**，服务端只存 ``sha256``；
- 列表接口不返回明文也不返回哈希（哈希外泄可被离线爆破）；
- 所有写操作经 ``audit_write`` 落审计表。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from newsplatform.api.v1._audit import audit_write
from newsplatform.core.deps import PlatformSessionDep
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import AuthContext, require_admin
from newsplatform.models.schemas import ApiKeyCreate
from newsplatform.services.admin_service import AdminService, AuditService

__all__ = ["router"]

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin), Depends(enforce_rate_limit)],
)


# ----------------------------------------------------------------------
# API Key
# ----------------------------------------------------------------------
@router.get("/api-keys", summary="API Key 列表")
async def list_api_keys(session: PlatformSessionDep) -> dict:
    """列出全部 Key 的元信息（永不含明文与哈希）。"""
    items = await AdminService(session).list_keys()
    return ok({"items": [i.model_dump(mode="json") for i in items], "total": len(items)})


@router.post("/api-keys", summary="创建 API Key")
async def create_api_key(
    request: Request,
    session: PlatformSessionDep,
    payload: ApiKeyCreate,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """创建一个 API Key，**明文仅在本次响应返回**，请立即保存。

    可用 scope：``articles:read``、``sources:read``、``sources:write``、
    ``stats:read``、``admin``（``admin`` 隐含全部权限）。
    """
    async with audit_write(
        session, request, auth, detail=f"create api-key {payload.name}"
    ):
        created = await AdminService(session).create_key(
            name=payload.name, scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
        )
    return ok(created.model_dump(mode="json"))


@router.post("/api-keys/{key_id}/enable", summary="启用 API Key")
async def enable_api_key(
    request: Request,
    session: PlatformSessionDep,
    key_id: int,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """重新启用被停用的 Key。"""
    async with audit_write(session, request, auth, detail=f"enable api-key {key_id}"):
        item = await AdminService(session).set_enabled(key_id, True)
    return ok(item.model_dump(mode="json"))


@router.post("/api-keys/{key_id}/disable", summary="停用 API Key")
async def disable_api_key(
    request: Request,
    session: PlatformSessionDep,
    key_id: int,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """停用 Key（比删除温和：审计日志里仍能对上是哪个 Key 做的操作）。"""
    async with audit_write(session, request, auth, detail=f"disable api-key {key_id}"):
        item = await AdminService(session).set_enabled(key_id, False)
    return ok(item.model_dump(mode="json"))


@router.delete("/api-keys/{key_id}", summary="删除 API Key")
async def delete_api_key(
    request: Request,
    session: PlatformSessionDep,
    key_id: int,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """彻底删除 Key（不可恢复）。"""
    async with audit_write(session, request, auth, detail=f"delete api-key {key_id}"):
        await AdminService(session).delete_key(key_id)
    return ok({"deleted": key_id})


# ----------------------------------------------------------------------
# 审计日志
# ----------------------------------------------------------------------
@router.get("/audit-logs", summary="审计日志查询")
async def list_audit_logs(
    session: PlatformSessionDep,
    path: Annotated[str | None, Query(description="路径模糊匹配，如 /sources")] = None,
    api_key_id: Annotated[int | None, Query(ge=1, description="按 Key 过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """按时间倒序查写操作审计（谁、何时、对哪个路径、什么结果、耗时多少）。"""
    items = await AuditService(session).list(path=path, api_key_id=api_key_id, limit=limit)
    return ok({"items": [i.model_dump(mode="json") for i in items], "total": len(items)})


@router.post("/audit-logs/purge", summary="清理历史审计")
async def purge_audit_logs(
    request: Request,
    session: PlatformSessionDep,
    keep_days: Annotated[int, Query(ge=1, le=3650, description="保留最近多少天")] = 90,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """删除 ``keep_days`` 天之前的审计记录（保留策略由运维定时调用）。"""
    async with audit_write(session, request, auth, detail=f"purge audit keep={keep_days}"):
        removed = await AuditService(session).purge(keep_days=keep_days)
    return ok({"removed": removed, "keep_days": keep_days})
