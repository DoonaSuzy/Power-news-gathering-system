"""文章查询仓储（对应改造方案 §3.6 的 ``/api/v1/articles`` 系列）。

筛选语义**必须与采集层的 ``newsagg/query.py`` 保持一致**——中台是采集数据的
对外出口，同一个 ``?province=440000&since=2026-10-01`` 在 CLI 导出与 API 里
应当命中同一批行，否则「看板数字与导出对不上」这类问题会反复出现。
因此这里逐条对齐了对方的 WHERE 条件与时间字段选择。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.models.orm import Article, Province, Source
from newsplatform.repositories.base import BaseRepository, Page, escape_like

__all__ = ["ArticleQuery", "ArticleRepository"]


@dataclass(slots=True)
class ArticleQuery:
    """文章查询条件（与 ``newsagg.query.ArticleFilter`` 字段对齐）。"""

    #: 省份代码**列表**。为什么是列表而不是单值：大区名（华南 / 港澳台）
    #: 一对多，一次查多省是很自然的用法（见 ``core/provinces.py``）。
    #: ``None`` = 不加省份过滤。
    province_codes: tuple[str, ...] | None = None
    category: str | None = None
    adapter_name: str | None = None
    #: 站名模糊匹配（``Source.name``，人可读，如「国网北京市电力公司」）。
    #: 与 ``adapter_name`` 的区别：后者是程序用的适配器标识（``sgcc_bj``）。
    source_name: str | None = None
    source_id: int | None = None
    #: 北京时间（naive），与 ``articles.published_at_cst`` 同类型比较
    date_from: datetime | None = None
    date_to: datetime | None = None
    keyword: str | None = None
    body_only: bool = False
    exclude_duplicates: bool = False
    page: int = 1
    page_size: int = 20


class ArticleRepository(BaseRepository):
    """文章读取。"""

    #: 列表返回列（正文只取前 200 字做摘要，避免列表接口拖走大字段）
    _LIST_COLUMNS = (
        Article.id,
        Article.title,
        Article.published_at_cst,
        Article.time_precision,
        Article.url,
        Article.source_name,
        Article.quality_flags,
        Source.adapter_name,
        Source.name.label("source_name_full"),
        Source.province_code,
        Source.category,
        Province.name.label("province_name"),
        func.left(func.coalesce(Article.body_text, ""), 200).label("excerpt"),
    )

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ---------------- 条件 ----------------
    @staticmethod
    def conditions(q: ArticleQuery) -> list[Any]:
        """构造 WHERE 条件列表（列表接口与导出共用）。"""
        conds: list[Any] = []
        if q.province_codes:
            # 单省时 in_ 只含一个元素，MySQL 仍走 province_code 索引。
            # 统一成一个分支，避免「单值走 = 、多值走 in_」两套逻辑漂移。
            conds.append(Source.province_code.in_(tuple(q.province_codes)))
        if q.category:
            conds.append(Source.category == q.category)
        if q.adapter_name:
            conds.append(Source.adapter_name == q.adapter_name)
        if q.source_name:
            conds.append(Source.name.like(f"%{escape_like(q.source_name)}%", escape="\\"))
        if q.source_id:
            conds.append(Article.source_id == int(q.source_id))
        if q.date_from:
            conds.append(Article.published_at_cst >= q.date_from)
        if q.date_to:
            conds.append(Article.published_at_cst <= q.date_to)
        if q.keyword:
            # 通配符按字面处理：搜「100%新能源」不该把 % 当任意字符（见 escape_like）
            like = f"%{escape_like(q.keyword)}%"
            conds.append(
                or_(
                    Article.title.like(like, escape="\\"),
                    Article.body_text.like(like, escape="\\"),
                )
            )
        if q.body_only:
            conds.append(and_(Article.body_text.is_not(None), Article.body_text != ""))
        if q.exclude_duplicates:
            conds.append(Article.quality_flags.not_like("%duplicate_of:%"))
        return conds

    def _base_select(self, q: ArticleQuery) -> Select:
        return (
            select(*self._LIST_COLUMNS)
            .join(Source.__table__, Source.id == Article.source_id)
            .join(Province.__table__, Province.code == Source.province_code, isouter=True)
            .where(*self.conditions(q))
        )

    def _count_select(self, q: ArticleQuery) -> Select:
        """仅 count：不取列、不 join 省份表，少一次联表开销。"""
        return (
            select(func.count())
            .select_from(Article.__table__.join(Source.__table__, Source.id == Article.source_id))
            .where(*self.conditions(q))
        )

    def _paginate_count_source(self, q: ArticleQuery) -> Any:
        """给 ``paginate`` 用的**行来源**子查询。

        ⚠️ 这里必须返回「产出行」的 SELECT（只取主键即可），**不能**直接传
        ``_count_select()``。因为 ``paginate`` 会在传进来的东西外面再套一层
        ``COUNT(*)``：

            SELECT count(*) FROM (<count_from>)

        如果 ``<count_from>`` 本身已经是 ``SELECT count(*) ...``（恒返回 1 行），
        外层统计到的就是「1 行」而不是「命中 N 行」——列表接口的 ``total``
        会静默恒等于 1，而 ``/articles/count`` 走 ``_count_select()`` 又是对的，
        两个接口给出互相矛盾的数字，极难排查。

        只取 ``Article.id``：既让 ``COUNT(*)`` 只扫索引，又不把 ``body_text``
        这种大字段带进派生表。
        """
        return (
            select(Article.id)
            .select_from(Article.__table__.join(Source.__table__, Source.id == Article.source_id))
            .where(*self.conditions(q))
            .subquery()
        )

    # ---------------- 查询 ----------------
    async def list(self, q: ArticleQuery) -> Page:
        """分页列出文章（按发布时间倒序）。"""
        stmt = self._base_select(q).order_by(
            Article.published_at_cst.desc(), Article.id.desc()
        )
        return await self.paginate(
            stmt,
            page=q.page,
            page_size=q.page_size,
            count_from=self._paginate_count_source(q),
        )

    async def get(self, article_id: int) -> dict[str, Any] | None:
        """取单篇详情（含正文全文）。"""
        stmt = (
            select(
                Article.id,
                Article.title,
                Article.body_text,
                Article.source_name,
                Article.published_at_utc,
                Article.published_at_cst,
                Article.time_precision,
                Article.url,
                Article.content_hash,
                Article.quality_flags,
                Article.created_at,
                Article.updated_at,
                Source.adapter_name,
                Source.name.label("source_name_full"),
                Source.province_code,
                Source.category,
                Province.name.label("province_name"),
            )
            .join(Source.__table__, Source.id == Article.source_id)
            .join(Province.__table__, Province.code == Source.province_code, isouter=True)
            .where(Article.id == int(article_id))
            .limit(1)
        )
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def list_by_province(self, province_code: str, q: ArticleQuery) -> Page:
        """按省份查文章（``/api/v1/provinces/{code}/articles`` 复用）。

        ``province_code`` 是已经解析过的**具体代码**（路径参数里允许写名称，
        但解析发生在路由层，走到这里必须是代码）。
        """
        # slots dataclass 没有 __dict__，必须用 dataclasses.replace 派生新实例
        return await self.list(replace(q, province_codes=(province_code,)))

    async def count(self, q: ArticleQuery) -> int:
        """仅统计条数。"""
        return int((await self.session.execute(self._count_select(q))).scalar() or 0)

    async def iter_export_rows(
        self, q: ArticleQuery, *, batch: int = 500, offset: int = 0
    ) -> Sequence[dict[str, Any]]:
        """取导出列的一批（由 service 用 ``offset`` 逐批推进）。

        ⚠️ ``offset`` 必须由调用方按已取行数推进。早先的实现只写 ``limit(batch)``
        而忽略翻页，调用方又是「取到不足一批才停」的循环，结果每轮都拿回**同一批**
        行——一次导出 50000 行里只有 500 行是真实数据，其余是 100 份重复，
        CSV 体积可达数百 MB。这类「循环取数却不推进游标」的写法在批量导出里
        是最容易漏的坑，故此处把 ``offset`` 显式暴露在签名上，而不是藏在
        ``ArticleQuery.page`` 里让人以为不传也行。
        """
        stmt = (
            select(
                Article.id,
                Province.name.label("province_name"),
                Source.province_code,
                Source.category,
                Source.name.label("source_name_full"),
                Article.title,
                Article.body_text,
                Article.published_at_cst,
                Article.time_precision,
                Article.url,
                Article.quality_flags,
            )
            .join(Source.__table__, Source.id == Article.source_id)
            .join(Province.__table__, Province.code == Source.province_code, isouter=True)
            .where(*self.conditions(q))
            .order_by(Article.published_at_cst.desc(), Article.id.desc())
            .limit(batch)
            .offset(max(0, int(offset)))
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def latest_published_at(self) -> datetime | None:
        """最近一篇的发布时间（健康检查/新鲜度看板用）。"""
        return (await self.session.execute(select(func.max(Article.published_at_cst)))).scalar()
