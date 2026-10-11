"""文章检索：按省份 / 类别 / 日期 / 关键词 / 来源查询。

分页在 **SQL 层一次完成**（``LIMIT ... OFFSET``），不要再在 Python 侧切片，
否则会出现「双截断」导致少返数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from newsagg.db import Database

__all__ = ["ArticleFilter", "build_where", "query_articles", "get_article", "count_articles"]

_MAX_PAGE_SIZE = 100


@dataclass(slots=True)
class ArticleFilter:
    """文章查询条件。"""

    province_code: str | None = None
    category: str | None = None
    source_id: int | None = None
    adapter_name: str | None = None
    date_from: datetime | None = None      # 北京时间
    date_to: datetime | None = None        # 北京时间
    keyword: str | None = None
    body_only: bool = False
    exclude_duplicates: bool = False
    page: int = 1
    page_size: int = 20

    def normalized(self) -> ArticleFilter:
        """把分页参数收敛到合法范围。"""
        self.page = max(1, int(self.page or 1))
        self.page_size = max(1, min(_MAX_PAGE_SIZE, int(self.page_size or 20)))
        return self


def build_where(f: ArticleFilter) -> tuple[str, list[Any]]:
    """构造 WHERE 子句与参数（query / export 共用，保证筛选语义一致）。"""
    clauses: list[str] = []
    params: list[Any] = []

    if f.province_code:
        clauses.append("s.province_code = %s")
        params.append(f.province_code)
    if f.category:
        clauses.append("s.category = %s")
        params.append(f.category)
    if f.source_id:
        clauses.append("a.source_id = %s")
        params.append(int(f.source_id))
    if f.adapter_name:
        clauses.append("s.adapter_name = %s")
        params.append(f.adapter_name)
    if f.date_from:
        clauses.append("a.published_at_cst >= %s")
        params.append(f.date_from)
    if f.date_to:
        clauses.append("a.published_at_cst <= %s")
        params.append(f.date_to)
    if f.keyword:
        like = f"%{f.keyword}%"
        clauses.append("(a.title LIKE %s OR a.body_text LIKE %s)")
        params.extend([like, like])
    if f.body_only:
        clauses.append("a.body_text IS NOT NULL AND a.body_text <> ''")
    if f.exclude_duplicates:
        clauses.append("a.quality_flags NOT LIKE %s")
        params.append("%duplicate_of:%")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


_BASE_JOIN = (
    "FROM articles a JOIN sources s ON s.id = a.source_id "
)

_COLUMNS = (
    "a.id, a.title, a.source_name, a.published_at_cst, a.url, "
    "LEFT(a.body_text, 200) AS excerpt, a.quality_flags, "
    "s.name AS source_name_full, s.province_code, s.category, "
    "p.name AS province_name "
)

_JOIN_PROVINCE = "LEFT JOIN provinces p ON p.code = s.province_code "


def query_articles(f: ArticleFilter, db: Database | None = None) -> dict[str, Any]:
    """分页查询文章。返回 ``{"total", "page", "page_size", "items"}``。"""
    f = f.normalized()
    db = db or Database()
    where, params = build_where(f)

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS n {_BASE_JOIN}{_JOIN_PROVINCE}{where}", params
            )
            total = int((cur.fetchone() or {}).get("n") or 0)

            offset = (f.page - 1) * f.page_size
            cur.execute(
                f"SELECT {_COLUMNS}{_BASE_JOIN}{_JOIN_PROVINCE}{where} "
                "ORDER BY a.published_at_cst DESC, a.id DESC LIMIT %s OFFSET %s",
                [*params, f.page_size, offset],
            )
            items = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return {
        "total": total,
        "page": f.page,
        "page_size": f.page_size,
        "pages": (total + f.page_size - 1) // f.page_size,
        "items": items,
    }


def count_articles(f: ArticleFilter, db: Database | None = None) -> int:
    """仅统计条数（导出前预览、统计接口复用）。"""
    db = db or Database()
    where, params = build_where(f)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n {_BASE_JOIN}{where}", params)
            return int((cur.fetchone() or {}).get("n") or 0)
    finally:
        conn.close()


def get_article(article_id: int, db: Database | None = None,
                *, with_body: bool = True) -> dict[str, Any] | None:
    """取单篇文章详情。"""
    db = db or Database()
    body = "a.body_text" if with_body else "'' AS body_text"
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT a.id, a.title, {body}, a.source_name, a.published_at_cst, "
                "a.published_at_utc, a.time_precision, a.url, a.content_hash, "
                "a.quality_flags, s.name AS source_name_full, s.adapter_name, "
                "s.province_code, s.category, p.name AS province_name "
                f"{_BASE_JOIN}{_JOIN_PROVINCE} WHERE a.id = %s",
                (article_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()
