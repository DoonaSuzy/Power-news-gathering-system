"""省级新闻库的只读查询（``channel=province``）。

设计约束
--------
1. **输出列必须与电力侧逐字对齐**，否则同一个 ``/articles`` 在 ``channel=power``
   与 ``channel=province`` 下返回的字段集不同，前端就得写两套渲染。
   对齐清单见 :data:`_LIST_COLUMNS` 与 ``models/schemas.ArticleListItem``。
2. **只读**。省级库的写入方永远是省级那套 ``newsagg``，中台不碰。
3. ``category`` 在省级库里没有对应概念，统一输出 ``province_media``，
   这样客户端拿到 ``category`` 就能区分数据域，不必依赖 ``channel`` 字段是否存在。

为什么不复用 ``ArticleRepository``
----------------------------------
那张表的 ``sources`` 列名不同（``media_name`` / ``column_url``），JOIN 条件
与 WHERE 都要重写；硬套一个仓储只会在里面塞满 ``if channel == ...``。
两个仓储、一个统一的 ``Page`` 出口，比一个到处分支的仓储好维护。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.models.province_orm import ProvinceArticle, ProvinceDict, ProvinceSource
from newsplatform.repositories.articles import ArticleQuery
from newsplatform.repositories.base import BaseRepository, Page, escape_like

__all__ = ["PROVINCE_CATEGORY", "ProvinceArticleRepository", "ProvinceSourceRepository"]

#: 省级数据域在 ``category`` 字段上的取值。
#: 不与电力侧的 sgcc/csg/... 混用，客户端据此可判断这条来自哪个域。
PROVINCE_CATEGORY = "province_media"

#: 正文摘要长度（与电力侧 ``_LIST_COLUMNS`` 保持一致）
_EXCERPT_CHARS = 200


def code_variants(code: str) -> tuple[str, ...]:
    """把省份代码展开成「该库可能存的所有写法」。

    为什么需要这个
    --------------
    电力库（``powernews``）用 **6 位**行政区划码（``440000``），
    省级库（``newsagg``）用 **2 位**（``44``）。合并后对外统一 6 位，
    但查省级库时必须降位，否则 ``WHERE province_code IN ('440000')``
    永远命中 0 行——**而且不报错**，这是最难发现的一类缺陷。

    两种写法都带上，是为了兼容「迁移到一半」的库：如果哪天省级数据补齐成
    6 位，这里的查询不需要再改一次。
    """
    text = str(code or "").strip()
    if len(text) == 6:
        return (text, text[:2])
    if len(text) == 2:
        return (text, f"{text}0000")
    return (text,) if text else ()


def pad_code(code: str | None) -> str | None:
    """把省级库的 2 位码补成 6 位，保证对外口径与电力域一致。"""
    if not code:
        return code
    text = str(code)
    return f"{text}0000" if len(text) == 2 else text


def _expand(codes: tuple[str, ...]) -> tuple[str, ...]:
    """批量展开并去重（保持稳定顺序，便于日志与 SQL 复用）。"""
    out: list[str] = []
    for code in codes:
        for variant in code_variants(code):
            if variant not in out:
                out.append(variant)
    return tuple(out)


class ProvinceArticleRepository(BaseRepository):
    """省级文章读取。"""

    _LIST_COLUMNS = (
        ProvinceArticle.id,
        ProvinceArticle.title,
        ProvinceArticle.published_at_cst,
        ProvinceArticle.time_precision,
        ProvinceArticle.url,
        ProvinceArticle.source_name,
        ProvinceArticle.quality_flags,
        ProvinceSource.adapter_name,
        ProvinceSource.media_name.label("source_name_full"),
        ProvinceSource.province_code,
        ProvinceDict.name.label("province_name"),
        func.left(
            func.coalesce(ProvinceArticle.body_text, ""), _EXCERPT_CHARS
        ).label("excerpt"),
    )

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ---------------- 条件 ----------------
    @staticmethod
    def conditions(q: ArticleQuery) -> list[Any]:
        """构造 WHERE 条件列表。

        ``category`` **不在这里处理**：省级域没有这个维度，传了应当在服务层
        直接 400，而不是静默返回 0 条（「传了但没生效」是最难排查的一类缺陷）。
        """
        conds: list[Any] = []
        if q.province_codes:
            # 6 位 → 同时匹配 2 位（省级库的存法），见 code_variants()
            conds.append(ProvinceSource.province_code.in_(_expand(q.province_codes)))
        if q.adapter_name:
            conds.append(ProvinceSource.adapter_name == q.adapter_name)
        if q.source_name:
            conds.append(
                ProvinceSource.media_name.like(f"%{escape_like(q.source_name)}%", escape="\\")
            )
        if q.source_id:
            conds.append(ProvinceArticle.source_id == int(q.source_id))
        if q.date_from:
            conds.append(ProvinceArticle.published_at_cst >= q.date_from)
        if q.date_to:
            conds.append(ProvinceArticle.published_at_cst <= q.date_to)
        if q.keyword:
            like = f"%{escape_like(q.keyword)}%"
            conds.append(
                or_(
                    ProvinceArticle.title.like(like, escape="\\"),
                    ProvinceArticle.body_text.like(like, escape="\\"),
                )
            )
        if q.body_only:
            conds.append(
                and_(
                    ProvinceArticle.body_text.is_not(None),
                    ProvinceArticle.body_text != "",
                )
            )
        if q.exclude_duplicates:
            conds.append(ProvinceArticle.quality_flags.not_like("%duplicate_of:%"))
        return conds

    def _joined(self) -> Any:
        return ProvinceArticle.__table__.join(
            ProvinceSource.__table__, ProvinceSource.id == ProvinceArticle.source_id
        )

    def _base_select(self, q: ArticleQuery) -> Select:
        return (
            select(*self._LIST_COLUMNS)
            .select_from(self._joined())
            .join(
                ProvinceDict.__table__,
                ProvinceDict.code == ProvinceSource.province_code,
                isouter=True,
            )
            .where(*self.conditions(q))
        )

    def _count_select(self, q: ArticleQuery) -> Select:
        return select(func.count()).select_from(self._joined()).where(*self.conditions(q))

    def _paginate_count_source(self, q: ArticleQuery) -> Any:
        """给 ``paginate`` 用的行来源（只取主键，避免 COUNT(*) 被联表放大）。"""
        return (
            select(ProvinceArticle.id)
            .select_from(self._joined())
            .where(*self.conditions(q))
            .subquery()
        )

    # ---------------- 查询 ----------------
    def _order(self, stmt: Select) -> Select:
        return stmt.order_by(
            ProvinceArticle.published_at_cst.desc(), ProvinceArticle.id.desc()
        )

    async def list(self, q: ArticleQuery) -> Page:
        page = await self.paginate(
            self._order(self._base_select(q)),
            page=q.page,
            page_size=q.page_size,
            count_from=self._paginate_count_source(q),
        )
        page.items = [self._shape(row) for row in page.items]
        return page

    async def top(self, q: ArticleQuery, limit: int) -> list[dict[str, Any]]:
        """取前 ``limit`` 条（跨域归并用，不做 SQL 分页）。"""
        stmt = self._order(self._base_select(q)).limit(max(1, int(limit)))
        rows = (await self.session.execute(stmt)).mappings().all()
        return [self._shape(dict(r)) for r in rows]

    async def count(self, q: ArticleQuery) -> int:
        return int((await self.session.execute(self._count_select(q))).scalar() or 0)

    async def get(self, article_id: int) -> dict[str, Any] | None:
        stmt = (
            select(
                ProvinceArticle.id,
                ProvinceArticle.title,
                ProvinceArticle.body_text,
                ProvinceArticle.source_name,
                ProvinceArticle.published_at_utc,
                ProvinceArticle.published_at_cst,
                ProvinceArticle.time_precision,
                ProvinceArticle.url,
                ProvinceArticle.content_hash,
                ProvinceArticle.quality_flags,
                ProvinceArticle.discovered_at_utc,
                ProvinceArticle.updated_at,
                ProvinceSource.adapter_name,
                ProvinceSource.media_name.label("source_name_full"),
                ProvinceSource.province_code,
                ProvinceDict.name.label("province_name"),
            )
            .select_from(self._joined())
            .join(
                ProvinceDict.__table__,
                ProvinceDict.code == ProvinceSource.province_code,
                isouter=True,
            )
            .where(ProvinceArticle.id == int(article_id))
            .limit(1)
        )
        row = (await self.session.execute(stmt)).mappings().first()
        return self._shape(dict(row)) if row else None

    async def iter_export_rows(
        self, q: ArticleQuery, *, batch: int = 500, offset: int = 0
    ) -> Sequence[dict[str, Any]]:
        """取导出列的一批。``offset`` 必须由调用方按已取行数推进。"""
        stmt = (
            select(
                ProvinceArticle.id,
                ProvinceDict.name.label("province_name"),
                ProvinceSource.province_code,
                ProvinceSource.media_name.label("source_name_full"),
                ProvinceArticle.title,
                ProvinceArticle.body_text,
                ProvinceArticle.published_at_cst,
                ProvinceArticle.time_precision,
                ProvinceArticle.url,
                ProvinceArticle.quality_flags,
            )
            .select_from(self._joined())
            .join(
                ProvinceDict.__table__,
                ProvinceDict.code == ProvinceSource.province_code,
                isouter=True,
            )
            .where(*self.conditions(q))
            .order_by(ProvinceArticle.published_at_cst.desc(), ProvinceArticle.id.desc())
            .limit(batch)
            .offset(max(0, int(offset)))
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        return [self._shape(dict(r)) for r in rows]

    async def latest_published_at(self) -> datetime | None:
        return (
            await self.session.execute(select(func.max(ProvinceArticle.published_at_cst)))
        ).scalar()

    async def total(self) -> int:
        """全库条数（供 ``/channels`` 目录用）。"""
        return int(
            (
                await self.session.execute(
                    select(func.count()).select_from(ProvinceArticle.__table__)
                )
            ).scalar()
            or 0
        )

    # ---------------- 整形 ----------------
    @staticmethod
    def _shape(row: dict[str, Any]) -> dict[str, Any]:
        """补齐电力侧有、省级侧没有的列。

        - ``category``：省级库无此列，统一 ``province_media``；
        - ``channel``：让同一份列表能直接标出来源域；
        - ``quality_flags``：省级库里可能是空串（旧数据默认值），
          统一成 ``[]``，避免前端 ``JSON.parse('')`` 抛错。
        """
        row["category"] = PROVINCE_CATEGORY
        row["channel"] = "province"
        # 省级库存 2 位码，对外统一 6 位（与电力域、与前端下拉的取值一致）
        row["province_code"] = pad_code(row.get("province_code"))
        flags = row.get("quality_flags")
        if isinstance(flags, (list, dict)):
            # 采集库是 JSON 列，驱动可能直接反序列化；统一转成字符串
            flags = json.dumps(flags, ensure_ascii=False)
        row["quality_flags"] = flags or "[]"
        return row


class ProvinceSourceRepository(BaseRepository):
    """省级来源与省份覆盖度（只读）。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def list(
        self,
        *,
        province_codes: tuple[str, ...] | None = None,
        enabled: bool | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Page:
        conds: list[Any] = []
        if province_codes:
            conds.append(ProvinceSource.province_code.in_(_expand(tuple(province_codes))))
        if enabled is not None:
            # 采集库用 verification_status 表达启停（candidate/verified/enabled/paused），
            # 没有布尔列
            conds.append(
                ProvinceSource.verification_status == ("enabled" if enabled else "paused")
            )
        if keyword:
            like = f"%{escape_like(keyword)}%"
            conds.append(
                or_(
                    ProvinceSource.media_name.like(like, escape="\\"),
                    ProvinceSource.adapter_name.like(like, escape="\\"),
                )
            )

        stmt = (
            select(
                ProvinceSource.id,
                ProvinceSource.adapter_name,
                ProvinceSource.media_name.label("name"),
                ProvinceSource.province_code,
                ProvinceDict.name.label("province_name"),
                ProvinceSource.column_url.label("list_url"),
                ProvinceSource.verification_status,
                ProvinceSource.last_discovery_success_at_utc,
            )
            .select_from(
                ProvinceSource.__table__.outerjoin(
                    ProvinceDict.__table__, ProvinceDict.code == ProvinceSource.province_code
                )
            )
            .where(*conds)
            .order_by(ProvinceSource.province_code.asc(), ProvinceSource.id.asc())
        )
        count_from = (
            select(ProvinceSource.id).where(*conds).subquery()
            if conds
            else ProvinceSource.__table__
        )
        page_obj = await self.paginate(
            stmt, page=page, page_size=page_size, count_from=count_from
        )
        page_obj.items = [self._shape_source(r) for r in page_obj.items]
        return page_obj

    async def total(self) -> int:
        """全库来源数（供 ``/channels`` 目录用）。"""
        return int(
            (
                await self.session.execute(
                    select(func.count()).select_from(ProvinceSource.__table__)
                )
            ).scalar()
            or 0
        )

    async def by_source(
        self,
        *,
        province_codes: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按来源统计产出（对齐电力侧 ``StatsRepository.by_source`` 的字段）。

        省级库没有 ``enabled`` 布尔列，用 ``verification_status`` 折算；
        ``category`` 统一给 ``province_media``，让前端可以用同一套渲染。
        """
        conds: list[Any] = []
        if province_codes:
            conds.append(ProvinceSource.province_code.in_(_expand(province_codes)))

        stmt = (
            select(
                ProvinceSource.id,
                ProvinceSource.adapter_name,
                ProvinceSource.media_name.label("name"),
                ProvinceSource.province_code,
                ProvinceSource.verification_status,
                func.count(ProvinceArticle.id).label("article_count"),
                func.sum(case((ProvinceArticle.title == "", 1), else_=0)).label("empty_title"),
                func.sum(
                    case(
                        (
                            or_(
                                ProvinceArticle.body_text.is_(None),
                                ProvinceArticle.body_text == "",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("empty_body"),
                func.sum(
                    case((ProvinceArticle.published_at_cst.is_(None), 1), else_=0)
                ).label("no_time"),
                func.max(ProvinceArticle.published_at_cst).label("last_published_at"),
            )
            .select_from(
                ProvinceSource.__table__.join(
                    ProvinceArticle.__table__,
                    ProvinceArticle.source_id == ProvinceSource.id,
                    isouter=True,
                )
            )
            .where(*conds)
            .group_by(ProvinceSource.id)
            .order_by(func.count(ProvinceArticle.id).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            status = row.pop("verification_status", None)
            row["province_code"] = pad_code(row.get("province_code"))
            row["category"] = PROVINCE_CATEGORY
            row["channel"] = "province"
            row["status"] = status or "enabled"
            row["enabled"] = status == "enabled"
            row["priority"] = 0
            row["crawl_interval_hours"] = 0
            out.append(row)
        return out

    async def list_provinces(self) -> list[dict[str, Any]]:
        """省级库自带的省份字典（31 个大陆省级行政区）。

        只返回 ``code`` / ``name``：省级库里没有 ``short_name`` / ``region`` /
        ``is_special`` 这些展示字段，跨域合并时以电力侧那份为准。
        """
        stmt = select(ProvinceDict.code, ProvinceDict.name).order_by(ProvinceDict.code.asc())
        rows = (await self.session.execute(stmt)).mappings().all()
        return [{"code": pad_code(r["code"]), "name": r["name"]} for r in rows]

    async def coverage(self) -> list[dict[str, Any]]:
        """按省份聚合：来源数 / 启用数 / 文章数 / 空正文数 / 最近发布时间。

        用 ``LEFT JOIN`` 从**省份字典**出发，保证没有来源的省份也出现在结果里
        （看板需要看到缺口，而不是缺口凭空消失）。
        """
        empty_body = func.sum(
            case(
                (
                    or_(
                        ProvinceArticle.body_text.is_(None),
                        ProvinceArticle.body_text == "",
                    ),
                    1,
                ),
                else_=0,
            )
        )
        stmt = (
            select(
                ProvinceDict.code,
                ProvinceDict.name,
                func.count(func.distinct(ProvinceSource.id)).label("source_count"),
                func.count(
                    func.distinct(
                        case(
                            (ProvinceSource.verification_status == "enabled", ProvinceSource.id),
                            else_=None,
                        )
                    )
                ).label("enabled_source_count"),
                func.count(ProvinceArticle.id).label("article_count"),
                func.coalesce(empty_body, 0).label("empty_body"),
                func.max(ProvinceArticle.published_at_cst).label("last_published_at"),
            )
            .select_from(
                ProvinceDict.__table__.outerjoin(
                    ProvinceSource.__table__,
                    ProvinceSource.province_code == ProvinceDict.code,
                ).outerjoin(
                    ProvinceArticle.__table__,
                    ProvinceArticle.source_id == ProvinceSource.id,
                )
            )
            .group_by(ProvinceDict.code, ProvinceDict.name)
            .order_by(ProvinceDict.code.asc())
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        # 对外统一 6 位码：跨域合并要按 code 对齐电力侧的 34 省
        return [{**dict(r), "code": pad_code(r["code"])} for r in rows]

    @staticmethod
    def _shape_source(row: dict[str, Any]) -> dict[str, Any]:
        """对齐电力侧 ``SourceItem`` 的字段集。"""
        row["category"] = PROVINCE_CATEGORY
        row["channel"] = "province"
        row["province_code"] = pad_code(row.get("province_code"))
        row["priority"] = 0
        row["crawl_interval_hours"] = 0
        status = row.pop("verification_status", None)
        row["status"] = status or "enabled"
        row["enabled"] = status == "enabled"
        row["note"] = row.get("pause_reason") or ""
        row["cooldown_until_utc"] = row.get("cooldown_until_utc")
        return row
