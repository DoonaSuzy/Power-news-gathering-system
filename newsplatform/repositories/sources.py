"""来源与省份仓储。

写入范围（重要）
----------------
中台对采集库**只允许改配置列**：``enabled`` / ``list_url`` / ``crawl_interval_hours`` /
``priority`` / ``note``。产出行（``articles`` / ``crawl_jobs`` / ``article_tasks`` /
``lease_*``）一律由采集层独占写，中台碰它们是数据竞争的源头。
这条边界由本模块的 ``UPDATABLE_FIELDS`` 白名单在代码层强制。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import Conflict, SourceNotFound
from newsplatform.models.orm import Article, CrawlJob, Province, Source
from newsplatform.repositories.base import BaseRepository, Page, escape_like

__all__ = ["SourceRepository", "ProvinceRepository"]


class SourceRepository(BaseRepository):
    """来源读取与配置更新。"""

    #: 允许中台修改的列白名单
    UPDATABLE_FIELDS = (
        "enabled",
        "list_url",
        "crawl_interval_hours",
        "priority",
        "note",
        "status",
    )

    _LIST_COLUMNS = (
        Source.id,
        Source.adapter_name,
        Source.name,
        Source.province_code,
        Source.category,
        Source.priority,
        Source.crawl_interval_hours,
        Source.enabled,
        Source.status,
        Source.list_url,
        Source.note,
        Source.cooldown_until_utc,
        Source.last_discovery_success_at_utc,
        Province.name.label("province_name"),
    )

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    def _base_select(self):
        return (
            select(*self._LIST_COLUMNS)
            .join(Province.__table__, Province.code == Source.province_code, isouter=True)
        )

    async def total(self) -> int:
        """全库来源数（供 ``/channels`` 目录用）。"""
        return int(
            (await self.session.execute(select(func.count()).select_from(Source.__table__)))
            .scalar()
            or 0
        )

    async def list(
        self,
        *,
        province_codes: tuple[str, ...] | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        """列出来源。

        ``province_codes`` 是**代码列表**（大区名会展开成多码，见 ``core/provinces.py``）；
        ``keyword`` 对人可读字段（名称 / 适配器名 / 站点 id）做模糊匹配——
        ``Source.name`` 是「国网北京市电力公司」这种，让人去记 ``sgcc_bj`` 并不合理。
        """
        stmt = self._base_select()
        conds = []
        if province_codes:
            conds.append(Source.province_code.in_(tuple(province_codes)))
        if category:
            conds.append(Source.category == category)
        if enabled is not None:
            conds.append(Source.enabled == enabled)
        if keyword:
            like = f"%{escape_like(keyword)}%"
            conds.append(
                or_(
                    Source.name.like(like, escape="\\"),
                    Source.adapter_name.like(like, escape="\\"),
                    Source.source_id.like(like, escape="\\"),
                )
            )
        stmt = stmt.where(*conds).order_by(
            Source.priority.asc(), Source.province_code.asc(), Source.id.asc()
        )
        return await self.paginate(stmt, page=page, page_size=page_size)

    async def get(self, source_id: int) -> dict[str, Any]:
        """按 id 取来源，不存在抛 :class:`SourceNotFound`。"""
        stmt = self._base_select().where(Source.id == int(source_id)).limit(1)
        row = (await self.session.execute(stmt)).mappings().first()
        if not row:
            raise SourceNotFound(f"来源不存在: {source_id}")
        return dict(row)

    async def get_by_adapter(self, adapter_name: str) -> dict[str, Any] | None:
        """按适配器名取来源（导入/幂等判断用）。"""
        stmt = self._base_select().where(Source.adapter_name == adapter_name).limit(1)
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def create(self, payload: dict[str, Any]) -> int:
        """新增来源，返回新 id。适配器名重复抛 :class:`Conflict`。

        注意：新增来源**必须**同时有对应的适配器文件（``newsagg/adapters/``），
        否则协调器建批后引擎找不到解析规则，任务会以 ``AdapterNotFound`` 失败。
        中台只能登记配置，无法生成适配器代码。
        """
        adapter_name = (payload.get("adapter_name") or "").strip()
        if not adapter_name:
            raise Conflict("adapter_name 不能为空")
        if await self.get_by_adapter(adapter_name):
            raise Conflict(f"适配器已存在: {adapter_name}")

        row = {
            "adapter_name": adapter_name,
            "source_id": payload.get("source_id") or adapter_name,
            "name": payload.get("name") or adapter_name,
            "province_code": payload.get("province_code"),
            "base_url": payload.get("base_url") or "",
            "news_path": payload.get("news_path") or "",
            "list_url": payload.get("list_url") or "",
            "category": payload.get("category") or "sgcc",
            "priority": int(payload.get("priority") or 0),
            "crawl_interval_hours": int(payload.get("crawl_interval_hours") or 1),
            "enabled": bool(payload.get("enabled", True)),
            "status": payload.get("status") or "active",
            "note": payload.get("note") or "",
        }
        result = await self.session.execute(insert(Source.__table__).values(**row))
        return int(result.inserted_primary_key[0])

    async def update(self, source_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """更新来源配置列（白名单外的键直接忽略）。"""
        await self.get(source_id)  # 不存在则抛 SourceNotFound

        changes = {
            k: v for k, v in payload.items()
            if k in self.UPDATABLE_FIELDS and v is not None
        }
        if "enabled" in changes:
            changes["enabled"] = bool(changes["enabled"])
        if changes:
            await self.session.execute(
                update(Source.__table__).where(Source.id == int(source_id)).values(**changes)
            )
            await self.session.flush()
        return await self.get(source_id)

    async def delete(self, source_id: int) -> None:
        """删除来源。存在文章时拒删（保数据可追溯），先停用即可。"""
        await self.get(source_id)
        n = int(
            (await self.session.execute(
                select(func.count()).select_from(Article.__table__)
                .where(Article.source_id == int(source_id))
            )).scalar() or 0
        )
        if n:
            raise Conflict(f"来源下已有 {n} 篇文章，不能删除；请改为 enabled=false")
        await self.session.execute(delete(Source.__table__).where(Source.id == int(source_id)))


class ProvinceRepository(BaseRepository):
    """省份字典读取与覆盖度统计。"""

    _LIST_COLUMNS = (
        Province.code,
        Province.name,
        Province.short_name,
        Province.region,
        Province.is_special,
    )

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def list(self) -> list[dict[str, Any]]:
        """全部 34 个省级行政区（按代码排序）。"""
        stmt = select(*self._LIST_COLUMNS).order_by(Province.code.asc())
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def get(self, code: str) -> dict[str, Any] | None:
        """按代码取省份。"""
        stmt = select(*self._LIST_COLUMNS).where(Province.code == code).limit(1)
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def coverage(self) -> list[dict[str, Any]]:
        """省份覆盖度：来源数 / 启用来源数 / 文章数 / 空正文数 / 最近发布时间。

        与 ``newsagg/stats.py::by_province`` 口径一致，供
        ``/api/v1/stats/provinces`` 与采集质量看板使用。
        """
        src = (
            select(
                Source.province_code.label("province_code"),
                func.count().label("source_count"),
                func.sum(case((Source.enabled.is_(True), 1), else_=0)).label(
                    "enabled_source_count"
                ),
            )
            .where(Source.province_code.is_not(None))
            .group_by(Source.province_code)
            .subquery()
        )
        art = (
            select(
                Source.province_code.label("province_code"),
                func.count().label("article_count"),
                func.sum(
                    case(
                        (
                            Article.body_text.is_(None) | (Article.body_text == ""),
                            1,
                        ),
                        else_=0,
                    )
                ).label("empty_body"),
                func.max(Article.published_at_cst).label("last_published_at"),
            )
            .select_from(Article.__table__.join(Source.__table__, Source.id == Article.source_id))
            .group_by(Source.province_code)
            .subquery()
        )

        stmt = (
            select(
                Province.code,
                Province.name,
                Province.short_name,
                Province.region,
                Province.is_special,
                func.coalesce(src.c.source_count, 0).label("source_count"),
                func.coalesce(src.c.enabled_source_count, 0).label("enabled_source_count"),
                func.coalesce(art.c.article_count, 0).label("article_count"),
                func.coalesce(art.c.empty_body, 0).label("empty_body"),
                art.c.last_published_at,
            )
            .select_from(Province.__table__)
            .join(src, src.c.province_code == Province.code, isouter=True)
            .join(art, art.c.province_code == Province.code, isouter=True)
            .order_by(func.coalesce(art.c.article_count, 0).desc(), Province.code.asc())
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def job_health(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """任务健康度（按状态聚合）。"""
        stmt = (
            select(
                CrawlJob.status,
                func.count().label("n"),
                func.round(func.avg(CrawlJob.discovered_count), 1).label("avg_discovered"),
                func.round(func.avg(CrawlJob.saved_count), 1).label("avg_saved"),
                func.max(CrawlJob.finished_at_utc).label("last_finished"),
            )
            .group_by(CrawlJob.status)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    @staticmethod
    def utcnow() -> datetime:
        """统一的 UTC naive 时间（与库内 ``*_utc`` 字段同型）。"""
        return datetime.now(UTC).replace(tzinfo=None)


def empty_page(page: int, page_size: int) -> Page:
    """空分页结果（筛选无命中时返回，避免 None 判空散落各处）。"""
    return Page([], 0, page, page_size)
