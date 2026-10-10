"""中台管理服务：API Key、审计日志。

安全要点
--------
- 明文 Key **只在创建时返回一次**，之后服务端只持有 ``sha256``；
- ``list`` 不返回哈希，避免哈希外泄后被离线爆破；
- 每次写操作由路由层调用 :meth:`AuditService.record`（改造方案 §6.3）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import BadRequest, ResourceNotFound
from newsplatform.core.security import generate_api_key, hash_api_key
from newsplatform.models.schemas import ApiKeyCreated, ApiKeyItem, AuditLogItem
from newsplatform.repositories.admin import ApiKeyRepository, AuditRepository

__all__ = ["AdminService", "AuditService"]

_ALLOWED_SCOPES = {
    "articles:read",
    "sources:read",
    "sources:write",
    "stats:read",
    "admin",
}


class AdminService:
    """API Key 生命周期管理。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ApiKeyRepository(session)

    async def list_keys(self) -> list[ApiKeyItem]:
        """列出全部 Key（不含明文/哈希）。"""
        return [ApiKeyItem(**row) for row in await self.repo.list()]

    async def create_key(
        self, *, name: str, scopes: str = "articles:read", expires_in_days: int | None = None
    ) -> ApiKeyCreated:
        """创建 Key 并**一次性**返回明文。"""
        requested = {s.strip() for s in (scopes or "").split(",") if s.strip()}
        invalid = requested - _ALLOWED_SCOPES
        if invalid:
            raise BadRequest(f"不支持的 scope: {', '.join(sorted(invalid))}")
        if not requested:
            raise BadRequest("scopes 不能为空")

        raw = generate_api_key()
        expires_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=int(expires_in_days))
            if expires_in_days
            else None
        )
        key_id = await self.repo.create(
            name=name,
            key_hash=hash_api_key(raw),
            scopes=",".join(sorted(requested)),
            expires_at=expires_at,
        )
        return ApiKeyCreated(
            id=key_id, name=name, key=raw,
            scopes=",".join(sorted(requested)), expires_at=expires_at,
        )

    async def set_enabled(self, key_id: int, enabled: bool) -> ApiKeyItem:
        """启用/停用 Key。"""
        if not await self.repo.set_enabled(key_id, enabled):
            raise ResourceNotFound(f"API Key 不存在: {key_id}")
        rows = await self.repo.list()
        for row in rows:
            if int(row["id"]) == int(key_id):
                return ApiKeyItem(**row)
        raise ResourceNotFound(f"API Key 不存在: {key_id}")

    async def delete_key(self, key_id: int) -> None:
        """删除 Key。"""
        if not await self.repo.delete(key_id):
            raise ResourceNotFound(f"API Key 不存在: {key_id}")


class AuditService:
    """审计日志读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AuditRepository(session)

    async def record(self, **kwargs: Any) -> None:
        """写审计记录。"""
        await self.repo.record(**kwargs)

    async def list(
        self, *, path: str | None = None, api_key_id: int | None = None, limit: int = 100
    ) -> list[AuditLogItem]:
        """查审计日志。"""
        rows = await self.repo.list(path=path, api_key_id=api_key_id, limit=limit)
        return [AuditLogItem(**row) for row in rows]

    async def purge(self, *, keep_days: int = 90) -> int:
        """清理 ``keep_days`` 天前的日志。"""
        if keep_days < 1:
            raise BadRequest("keep_days 必须 >= 1")
        before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=keep_days)
        return await self.repo.purge_before(before)
