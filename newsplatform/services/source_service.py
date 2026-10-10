"""来源与省份服务。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import ProvinceNotFound
from newsplatform.models.schemas import (
    ProvinceCoverageItem,
    ProvinceItem,
    SourceItem,
    SourcePage,
)
from newsplatform.repositories.sources import ProvinceRepository, SourceRepository

__all__ = ["SourceService", "ProvinceService"]


class SourceService:
    """来源管理。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SourceRepository(session)

    async def list(
        self,
        *,
        province_code: str | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> SourcePage:
        """分页列出来源。"""
        result = await self.repo.list(
            province_code=province_code, category=category,
            enabled=enabled, page=page, page_size=page_size,
        )
        return SourcePage(
            items=[SourceItem(**row) for row in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            pages=result.pages,
        )

    async def get(self, source_id: int) -> SourceItem:
        """取单个来源（不存在抛 SourceNotFound）。"""
        return SourceItem(**await self.repo.get(source_id))

    async def create(self, payload: dict) -> SourceItem:
        """新增来源。"""
        source_id = await self.repo.create(payload)
        return SourceItem(**await self.repo.get(source_id))

    async def update(self, source_id: int, payload: dict) -> SourceItem:
        """更新来源配置列。"""
        return SourceItem(**await self.repo.update(source_id, payload))

    async def set_enabled(self, source_id: int, enabled: bool) -> SourceItem:
        """启用/停用（最常用的运维动作，单独开一个语义化方法便于审计）。"""
        return await self.update(source_id, {"enabled": enabled})

    async def delete(self, source_id: int) -> None:
        """删除来源（有文章时拒绝）。"""
        await self.repo.delete(source_id)


class ProvinceService:
    """省份字典与覆盖度。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ProvinceRepository(session)

    async def list(self) -> list[ProvinceItem]:
        """34 个省级行政区。"""
        return [ProvinceItem(**row) for row in await self.repo.list()]

    async def get(self, code: str) -> ProvinceItem:
        """按代码取省份。"""
        row = await self.repo.get(code)
        if not row:
            raise ProvinceNotFound(f"省份不存在: {code}")
        return ProvinceItem(**row)

    async def coverage(self) -> list[ProvinceCoverageItem]:
        """省份覆盖度（含未覆盖的省份，方便看板显示缺口）。"""
        return [ProvinceCoverageItem(**row) for row in await self.repo.coverage()]

    async def job_health(self, *, limit: int = 20):
        """任务健康度。"""
        return await self.repo.job_health(limit=limit)
