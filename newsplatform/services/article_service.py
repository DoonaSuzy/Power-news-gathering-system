"""文章服务（改造方案 §2.3 services 层：业务逻辑落在这里）。

服务层的存在意义：让路由函数退化成「解析参数 → 调服务 → 包响应」。
校验、缓存、字段拼装、异常语义都在本层决定，路由改起来就不会牵动业务。

数据域（channel）
-----------------
中台把两套采集产物收敛成一个对外入口：

============  ==========================  ===============================
channel       数据源                       内容
============  ==========================  ===============================
``power``     ``powernews`` 库             电网公司 / 能源主管部门 / 行业媒体（60 源）
``province``  ``newsagg_platform`` 库      省级党报与广电官方新闻（34 源）
``all``       两者合并（默认）              ——
============  ==========================  ===============================

``province`` 是**可选**域：连接串为空时 ``channel=province`` 返回 503，
``channel=all`` 自动降级为 ``power`` 并在 ``ArticlePage.channels`` 里如实说明。
降级而不报错，是因为「没接省级库」不该让整个文章接口不可用；但也不能装作
什么都没发生——客户端必须能从响应里看出「这次只查了一个域」。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import (
    ArticleNotFound,
    BadRequest,
    ChannelUnavailable,
)
from newsplatform.models.schemas import (
    ArticleDetail,
    ArticleImage,
    ArticleListItem,
    ArticlePage,
)
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository
from newsplatform.repositories.province_articles import ProvinceArticleRepository

log = logging.getLogger("newsplatform.articles")

CHANNEL_POWER = "power"
CHANNEL_PROVINCE = "province"
CHANNEL_ALL = "all"
#: 合法取值。路由层用 ``Literal`` 校验，服务层仍要再校验一次——
#: 服务可能被 CLI / 脚本直接调用，不能假设调用方一定过了 FastAPI 的校验。
CHANNEL_CHOICES: tuple[str, ...] = (CHANNEL_POWER, CHANNEL_PROVINCE, CHANNEL_ALL)

#: 跨域归并的扫描上限：两库各取 ``page * page_size`` 条再切。
#: 3000 条数据下这个上限足够；超过它说明调用方在做深分页，应当改用导出。
MERGE_MAX_SCAN = 5000

#: 排序兜底值。``published_at_cst`` 可为 NULL（时间未解析出来），
#: 归并时统一沉到末尾，而不是让 ``None`` 参与比较抛 TypeError。
_MIN_DT = datetime.min


def _image_url(object_key: str) -> str:
    """把 RustFS 对象键拼成可直接访问的 URL。

    用 `s3_public_endpoint`（宿主机地址）而非容器内的 `s3_endpoint` ——
    前者客户端能访问，后者只在 Docker 网络内可达。
    """
    if not object_key:
        return ""
    # 函数内 import：避免 newsplatform ←→ newsagg 的模块级循环依赖
    from newsagg.config import get_settings  # noqa: PLC0415

    st = get_settings()
    base = (st.s3_public_endpoint or st.s3_endpoint or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{st.s3_bucket}/{object_key}"


__all__ = ["ArticleService", "EXPORT_COLUMNS", "CHANNEL_CHOICES"]

#: 导出列（中文表头，与采集层 ``newsagg/export.py`` 一致，便于两端口径对齐）
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "编号"),
    ("province_name", "省份"),
    ("province_code", "行政区划代码"),
    ("category", "来源类别"),
    ("source_name", "来源"),
    ("title", "标题"),
    ("body_text", "正文"),
    ("published_at_cst", "发布时间"),
    ("time_precision", "时间精度"),
    ("url", "原文链接"),
    ("quality_flags", "质量标记"),
)

#: 单次导出行数上限（防止一个请求把整库拉进内存）
EXPORT_MAX_ROWS = 50_000


class ArticleService:
    """文章查询与导出。

    Args:
        session: 电力采集库（``powernews``）会话。
        province_session: 省级新闻库（``newsagg_platform``）会话。
            ``None`` 表示未接入该数据域，相关 channel 会降级或报 503。
    """

    def __init__(
        self, session: AsyncSession, province_session: AsyncSession | None = None
    ) -> None:
        self.session = session
        self.repo = ArticleRepository(session)
        self.province_repo = (
            ProvinceArticleRepository(province_session) if province_session else None
        )

    # ---------------- 数据域 ----------------
    @property
    def available_channels(self) -> tuple[str, ...]:
        """当前实例真正可查的数据域（不含 ``all``）。"""
        if self.province_repo is None:
            return (CHANNEL_POWER,)
        return (CHANNEL_POWER, CHANNEL_PROVINCE)

    def _resolve(self, requested: str | None) -> tuple[str, ...]:
        """把用户请求的 channel 解析成**实际要查**的数据域元组。"""
        channel = (requested or CHANNEL_ALL).strip().lower()
        if channel not in CHANNEL_CHOICES:
            raise BadRequest(
                f"channel 取值必须是 {' / '.join(CHANNEL_CHOICES)} 之一，收到 {requested!r}"
            )
        if channel == CHANNEL_POWER:
            return (CHANNEL_POWER,)
        if channel == CHANNEL_PROVINCE:
            if self.province_repo is None:
                raise ChannelUnavailable(
                    "省级数据域未接入（POWERNEWS_PLATFORM_PROVINCE_DATABASE_URL 未配置）"
                )
            return (CHANNEL_PROVINCE,)
        # all：省级没接入就只查电力，并在响应里如实标注
        return self.available_channels

    def _repo_of(self, channel: str) -> ArticleRepository | ProvinceArticleRepository:
        if channel == CHANNEL_PROVINCE:
            assert self.province_repo is not None  # _resolve 已保证
            return self.province_repo
        return self.repo

    @staticmethod
    def _stamp(row: dict[str, Any], channel: str) -> dict[str, Any]:
        """给行打上数据域标记，并补齐跨域一致的默认值。"""
        row["channel"] = channel
        if not row.get("quality_flags"):
            row["quality_flags"] = "[]"
        if row.get("excerpt") is None:
            row["excerpt"] = ""
        return row

    # ---------------- 列表 ----------------
    async def list(self, q: ArticleQuery) -> ArticlePage:
        """分页列表。``channel=all`` 时跨库归并后切片。"""
        channels = self._resolve(q.channel)
        if len(channels) == 1:
            page = await self._repo_of(channels[0]).list(q)
            items = [self._stamp(row, channels[0]) for row in page.items]
            return ArticlePage(
                items=[ArticleListItem(**row) for row in items],
                total=page.total,
                page=page.page,
                page_size=page.page_size,
                pages=page.pages,
                channels=list(channels),
            )
        return await self._merge_list(q)

    async def _merge_list(self, q: ArticleQuery) -> ArticlePage:
        """跨域归并分页。

        为什么两库各取 ``page * page_size`` 条而不是各取 ``page_size`` 条：
        第 N 页的全局第 ``(N-1)*size+1`` 条，可能全部落在其中一个库里；
        只取一页的量会把「另一个库的前 N 页」整体漏掉。
        """
        scan = q.page * q.page_size
        if scan > MERGE_MAX_SCAN:
            raise BadRequest(
                f"跨域查询最多翻到第 {MERGE_MAX_SCAN // q.page_size} 页"
                f"（扫描上限 {MERGE_MAX_SCAN} 条）；请缩小时间范围或改用 /articles/export"
            )
        assert self.province_repo is not None  # _resolve 已保证
        power_rows, province_rows = await asyncio.gather(
            self.repo.top(q, scan),
            self.province_repo.top(q, scan),
        )
        power_total, province_total = await asyncio.gather(
            self.repo.count(q),
            self.province_repo.count(q),
        )
        merged = [self._stamp(r, CHANNEL_POWER) for r in power_rows]
        merged += [self._stamp(r, CHANNEL_PROVINCE) for r in province_rows]
        merged.sort(key=self._sort_key, reverse=True)

        start = (q.page - 1) * q.page_size
        items = merged[start : start + q.page_size]
        total = power_total + province_total
        return ArticlePage(
            items=[ArticleListItem(**row) for row in items],
            total=total,
            page=q.page,
            page_size=q.page_size,
            pages=(total + q.page_size - 1) // q.page_size if q.page_size else 0,
            channels=[CHANNEL_POWER, CHANNEL_PROVINCE],
        )

    @staticmethod
    def _sort_key(row: dict[str, Any]) -> tuple[datetime, str, int]:
        """全局倒序键：发布时间 → 数据域 → id。

        带 ``channel`` 是为了**稳定排序**：两库的 id 各自自增，跨库会出现相同 id，
        只按 ``(时间, id)`` 排会让同一条数据在两次请求里换位置（翻页时表现为
        「第 2 页又出现第 1 页的条目」）。
        """
        return (
            row.get("published_at_cst") or _MIN_DT,
            row.get("channel") or "",
            int(row.get("id") or 0),
        )

    # ---------------- 详情 ----------------
    async def detail(self, article_id: int, channel: str = CHANNEL_ALL) -> ArticleDetail:
        """文章详情，不存在抛 :class:`ArticleNotFound`。

        ``channel=all`` 时先查电力、再查省级——两个域的 id 各自自增，
        **id 相同不代表同一条**，所以不能"随便命中一个就返回"，
        必须按请求的域去找；``all`` 下按固定优先级（电力优先）取第一个命中。
        """
        channels = self._resolve(channel)
        row: dict[str, Any] | None = None
        hit_channel = channels[0]
        for name in channels:
            found = await self._repo_of(name).get(article_id)
            if found:
                row, hit_channel = found, name
                break
        if not row:
            raise ArticleNotFound(f"文章不存在: {article_id}（已查 {', '.join(channels)}）")

        detail = ArticleDetail(**self._stamp(row, hit_channel))
        # 配图只有电力库有（省级库没有 article_images 表），跨域时按域跳过。
        if hit_channel != CHANNEL_POWER:
            detail.images = []
            return detail
        # 配图：一次 IN 查询取回（不做 N+1）。
        #
        # 为什么要 try 兜住：配图是**附加信息**，查询失败不该让整个详情接口 500。
        # 但要**记 warning** —— 静默吞掉会让「images 一直空」变成查不出的悬案
        # （本次就遇到过：schema 有默认空列表，异常被吞后表现为「接口正常但没图」）。
        try:
            imgs = await self.repo.images_for_articles([article_id])
            detail.images = [
                ArticleImage(
                    **{**i, "object_url": _image_url(i.get("object_key", ""))}
                )
                for i in imgs.get(article_id, [])
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("配图查询失败（详情仍返回）: %s", exc)
            detail.images = []
        return detail

    # ---------------- 计数 ----------------
    async def count(self, q: ArticleQuery) -> int:
        """命中条数（导出前预览）。跨域时为两库之和。"""
        channels = self._resolve(q.channel)
        if len(channels) == 1:
            return await self._repo_of(channels[0]).count(q)
        assert self.province_repo is not None
        power, province = await asyncio.gather(
            self.repo.count(q), self.province_repo.count(q)
        )
        return power + province

    # ---------------- 导出 ----------------
    async def export_csv(self, q: ArticleQuery) -> dict[str, Any]:
        """导出 CSV 文本。

        返回 ``{"content": str, "rows": int, "truncated": bool}``；
        内容带 UTF-8 BOM，Excel 双击不乱码（与采集层导出行为一致）。
        """
        channels = self._resolve(q.channel)
        rows: list[dict[str, Any]] = []
        truncated = False

        for name in channels:
            batch_rows, hit_cap = await self._collect_rows(self._repo_of(name), q)
            rows.extend(self._stamp(r, name) for r in batch_rows)
            truncated = truncated or hit_cap
            if len(rows) >= EXPORT_MAX_ROWS:
                break

        # 跨域导出必须重新排序：两库各自有序，拼起来不是全局有序。
        if len(channels) > 1:
            rows.sort(key=self._sort_key, reverse=True)
        if len(rows) >= EXPORT_MAX_ROWS:
            truncated = True
            rows = rows[:EXPORT_MAX_ROWS]

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([label for _key, label in EXPORT_COLUMNS])
        for row in rows:
            if not row.get("source_name"):
                row["source_name"] = row.get("source_name_full") or ""
            writer.writerow([_cell(row.get(key)) for key, _label in EXPORT_COLUMNS])

        return {
            "content": "\ufeff" + buffer.getvalue(),
            "rows": len(rows),
            "truncated": truncated,
        }

    @staticmethod
    async def _collect_rows(
        repo: ArticleRepository | ProvinceArticleRepository, q: ArticleQuery
    ) -> tuple[list[dict[str, Any]], bool]:
        """按 500 一批拉取，直到取完或触顶。``offset`` 随已取行数推进。"""
        rows: list[dict[str, Any]] = []
        page_size = 500
        batch_query = ArticleQuery(
            province_codes=q.province_codes,
            category=q.category,
            adapter_name=q.adapter_name,
            source_name=q.source_name,
            source_id=q.source_id,
            date_from=q.date_from,
            date_to=q.date_to,
            keyword=q.keyword,
            body_only=q.body_only,
            exclude_duplicates=q.exclude_duplicates,
            page=1,
            page_size=page_size,
            channel=q.channel,
        )
        while len(rows) < EXPORT_MAX_ROWS:
            batch = await repo.iter_export_rows(
                batch_query, batch=page_size, offset=len(rows)
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
        return rows[:EXPORT_MAX_ROWS], len(rows) >= EXPORT_MAX_ROWS

    # ---------------- 新鲜度 ----------------
    async def latest_published_at(self) -> datetime | None:
        """最近一篇发布时间（健康检查用）。跨域取两者较大值。"""
        power = await self.repo.latest_published_at()
        if self.province_repo is None:
            return power
        province = await self.province_repo.latest_published_at()
        candidates = [d for d in (power, province) if d is not None]
        return max(candidates) if candidates else None


def _cell(value: Any) -> str:
    """把任意值转成 CSV 单元格字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
