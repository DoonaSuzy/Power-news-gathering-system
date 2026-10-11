"""中台自有库仓储：API Key、审计日志、统计快照。

作用域：``powernews_platform``（``PlatformBase``），唯一允许中台写入的库。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.models.orm import ApiKey, AuditLog, StatsSnapshot
from newsplatform.repositories.base import BaseRepository

__all__ = ["ApiKeyRepository", "AuditRepository", "SnapshotRepository"]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ApiKeyRepository(BaseRepository):
    """API Key 读取与维护。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """按 ``sha256(明文)`` 取 Key 记录。"""
        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash).limit(1)
        return (await self.session.execute(stmt)).scalars().first()

    async def count(self) -> int:
        """库内 Key 总数（决定是否启用引导令牌）。"""
        return int((await self.session.execute(
            select(func.count()).select_from(ApiKey.__table__)
        )).scalar() or 0)

    async def list(self) -> list[dict[str, Any]]:
        """列出全部 Key（**不返回明文，也不返回哈希**）。"""
        stmt = select(
            ApiKey.id, ApiKey.name, ApiKey.scopes, ApiKey.enabled,
            ApiKey.expires_at, ApiKey.last_used_at, ApiKey.created_at,
        ).order_by(ApiKey.id.desc())
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def create(self, *, name: str, key_hash: str, scopes: str = "articles:read",
                     expires_at: datetime | None = None) -> int:
        """写入一条 Key 记录。"""
        result = await self.session.execute(
            ApiKey.__table__.insert().values(
                name=name, key_hash=key_hash, scopes=scopes,
                enabled=True, expires_at=expires_at,
            )
        )
        return int(result.inserted_primary_key[0])

    async def set_enabled(self, key_id: int, enabled: bool) -> bool:
        """启用/停用。返回是否命中记录。"""
        result = await self.session.execute(
            update(ApiKey.__table__).where(ApiKey.id == int(key_id)).values(enabled=enabled)
        )
        return bool(result.rowcount)

    async def touch(self, key_id: int) -> None:
        """更新最近使用时间。"""
        await self.session.execute(
            update(ApiKey.__table__).where(ApiKey.id == int(key_id)).values(last_used_at=_utcnow())
        )

    async def delete(self, key_id: int) -> bool:
        """删除 Key。"""
        result = await self.session.execute(
            delete(ApiKey.__table__).where(ApiKey.id == int(key_id))
        )
        return bool(result.rowcount)


class AuditRepository(BaseRepository):
    """审计日志。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def record(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        status_code: int,
        api_key_id: int | None = None,
        api_key_name: str = "",
        client_ip: str = "",
        user_agent: str = "",
        duration_ms: int = 0,
        detail: str = "",
    ) -> None:
        """写一条审计记录（失败不抛——审计不该拖垮业务请求）。"""
        try:
            await self.session.execute(
                AuditLog.__table__.insert().values(
                    request_id=request_id[:32],
                    method=method[:10],
                    path=path[:255],
                    status_code=int(status_code),
                    api_key_id=api_key_id,
                    api_key_name=api_key_name[:64],
                    client_ip=client_ip[:64],
                    user_agent=user_agent[:255],
                    duration_ms=int(duration_ms),
                    detail=detail[:512],
                )
            )
        except Exception:  # noqa: BLE001 - 审计失败只记日志，不影响业务返回
            from newsplatform.core.logging import get_logger

            get_logger("audit").warning("审计日志写入失败（不影响业务）", exc_info=True)

    async def list(
        self,
        *,
        path: str | None = None,
        api_key_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查审计日志（按时间倒序）。"""
        stmt = select(
            AuditLog.id, AuditLog.request_id, AuditLog.api_key_id, AuditLog.api_key_name,
            AuditLog.method, AuditLog.path, AuditLog.status_code, AuditLog.client_ip,
            AuditLog.duration_ms, AuditLog.detail, AuditLog.created_at,
        )
        conds = []
        if path:
            conds.append(AuditLog.path.like(f"%{path}%"))
        if api_key_id:
            conds.append(AuditLog.api_key_id == int(api_key_id))
        stmt = stmt.where(*conds).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500)))
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def purge_before(self, before: datetime) -> int:
        """清理历史审计（保留策略由运维任务调用）。"""
        result = await self.session.execute(
            delete(AuditLog.__table__).where(AuditLog.created_at < before)
        )
        return int(result.rowcount or 0)


class SnapshotRepository(BaseRepository):
    """统计快照（按日物化，免去每次请求全表聚合）。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def upsert(
        self, *, snapshot_date: date, scope: str, scope_key: str, payload: dict[str, Any]
    ) -> None:
        """写入/更新快照（``uk_snapshot`` 唯一键驱动）。"""
        stmt = mysql_insert(StatsSnapshot.__table__).values(
            snapshot_date=snapshot_date,
            scope=scope,
            scope_key=scope_key,
            payload=payload,
        )
        stmt = stmt.on_duplicate_key_update(payload=stmt.inserted.payload)
        await self.session.execute(stmt)

    async def get(
        self, *, snapshot_date: date, scope: str, scope_key: str = ""
    ) -> dict[str, Any] | None:
        """读快照。"""
        stmt = select(StatsSnapshot.payload, StatsSnapshot.created_at).where(
            StatsSnapshot.snapshot_date == snapshot_date,
            StatsSnapshot.scope == scope,
            StatsSnapshot.scope_key == scope_key,
        ).limit(1)
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def latest(self, *, scope: str, scope_key: str = "") -> dict[str, Any] | None:
        """取该 scope 最新一份快照。"""
        stmt = (
            select(StatsSnapshot.snapshot_date, StatsSnapshot.payload, StatsSnapshot.created_at)
            .where(StatsSnapshot.scope == scope, StatsSnapshot.scope_key == scope_key)
            .order_by(StatsSnapshot.snapshot_date.desc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None
