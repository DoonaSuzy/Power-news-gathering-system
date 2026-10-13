"""文章服务（改造方案 §2.3 services 层：业务逻辑落在这里）。

服务层的存在意义：让路由函数退化成「解析参数 → 调服务 → 包响应」。
校验、缓存、字段拼装、异常语义都在本层决定，路由改起来就不会牵动业务。
"""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.exceptions import ArticleNotFound
from newsplatform.models.schemas import (
    ArticleDetail,
    ArticleListItem,
    ArticlePage,
)
from newsplatform.repositories.articles import ArticleQuery, ArticleRepository


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

__all__ = ["ArticleService", "EXPORT_COLUMNS"]

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
    """文章查询与导出。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ArticleRepository(session)

    async def list(self, q: ArticleQuery) -> ArticlePage:
        """分页列表。"""
        page = await self.repo.list(q)
        return ArticlePage(
            items=[ArticleListItem(**row) for row in page.items],
            total=page.total,
            page=page.page,
            page_size=page.page_size,
            pages=page.pages,
        )

    async def detail(self, article_id: int) -> ArticleDetail:
        """文章详情，不存在抛 :class:`ArticleNotFound`。"""
        row = await self.repo.get(article_id)
        if not row:
            raise ArticleNotFound(f"文章不存在: {article_id}")
        return ArticleDetail(**row)

    async def count(self, q: ArticleQuery) -> int:
        """命中条数（导出前预览）。"""
        return await self.repo.count(q)

    async def export_csv(self, q: ArticleQuery) -> dict[str, Any]:
        """导出 CSV 文本。

        返回 ``{"content": str, "rows": int, "truncated": bool}``；
        内容带 UTF-8 BOM，Excel 双击不乱码（与采集层导出行为一致）。
        """
        rows: list[dict[str, Any]] = []
        truncated = False
        page_size = 500

        # 分页循环拉取，直到取完或触顶（EXPORT_MAX_ROWS 防止一次请求拖垮内存）
        # 关键：offset 必须随已取行数推进，否则每轮拿回同一批数据。
        while len(rows) < EXPORT_MAX_ROWS:
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
            )
            batch = await self.repo.iter_export_rows(
                batch_query, batch=page_size, offset=len(rows)
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
        if len(rows) >= EXPORT_MAX_ROWS:
            truncated = True
            rows = rows[:EXPORT_MAX_ROWS]

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([label for _key, label in EXPORT_COLUMNS])
        for row in rows:
            if not row.get("source_name"):
                row["source_name"] = row.get("source_name_full") or ""
            writer.writerow([
                _cell(row.get(key)) for key, _label in EXPORT_COLUMNS
            ])

        return {
            "content": "\ufeff" + buffer.getvalue(),
            "rows": len(rows),
            "truncated": truncated,
        }

    async def latest_published_at(self):
        """最近一篇发布时间（健康检查用）。"""
        return await self.repo.latest_published_at()

def _cell(value: Any) -> str:
    """把任意值转成 CSV 单元格字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
