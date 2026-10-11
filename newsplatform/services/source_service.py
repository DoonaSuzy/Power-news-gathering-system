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
    """来源管理。

    事务归属：**服务层负责提交**
    --------------------------
    这是中台里唯一会写**采集库**的地方（``sources`` 的配置列）。三个写方法
    （``create`` / ``update`` / ``delete``）都在返回前显式 ``commit()``。

    为什么不把提交交给「采集库写会话」依赖在请求结束时统一做？

    1. **审计要准**。写路由的 ``audit_write`` 会在退出时按最终状态码落一条审计
       日志。若提交发生在依赖 teardown（也就是 ``audit_write`` 之后），
       一旦提交失败，日志已经写成「成功」——审计与事实相反，比没有审计更糟。
       把提交放进服务方法，就落在 ``audit_write`` 的作用域内，成功/失败都如实入库。
    2. **不依赖会话来源**。提交写在服务里，无论上游注入的是哪个会话，
       写都不会被静默回滚（本项目曾因注入只读会话而出现「返回 200 + 自增 id，
       表里却什么都没多」的静默失败）。
    3. ``CollectorWriteSessionDep`` 仍然保留为兜底：即使将来某个写方法漏了
       ``commit``，会话退出时也会补上。对同一会话重复 commit 是无害的空操作。

    读取（``list`` / ``get``）不做提交，走只读会话即可。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SourceRepository(session)

    async def list(
        self,
        *,
        province_codes: tuple[str, ...] | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> SourcePage:
        """分页列出来源。"""
        result = await self.repo.list(
            province_codes=province_codes, category=category,
            enabled=enabled, keyword=keyword,
            page=page, page_size=page_size,
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
        item = await self.repo.get(source_id)
        await self.session.commit()
        return SourceItem(**item)

    async def update(self, source_id: int, payload: dict) -> SourceItem:
        """更新来源配置列。"""
        row = await self.repo.update(source_id, payload)
        await self.session.commit()
        return SourceItem(**row)

    async def set_enabled(self, source_id: int, enabled: bool) -> SourceItem:
        """启用/停用（最常用的运维动作，单独开一个语义化方法便于审计）。"""
        return await self.update(source_id, {"enabled": enabled})

    async def delete(self, source_id: int) -> None:
        """删除来源（有文章时拒绝）。"""
        await self.repo.delete(source_id)
        await self.session.commit()


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
