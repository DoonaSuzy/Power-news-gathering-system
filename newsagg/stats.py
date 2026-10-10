"""采集统计：全局概览、按省份、按来源、字段质量、任务健康度。

这些聚合直接对应改造方案 §6.2 提到的「采集质量看板」，
数据中台的 ``/api/v1/stats/*`` 接口复用同一套 SQL 语义。
"""

from __future__ import annotations

from typing import Any

from newsagg.db import Database

__all__ = [
    "overview",
    "by_province",
    "by_source",
    "quality",
    "job_health",
    "render_text",
]


def _fetch_all(db: Database, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fetch_one(db: Database, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    rows = _fetch_all(db, sql, params)
    return rows[0] if rows else {}


def overview(db: Database | None = None) -> dict[str, Any]:
    """全局概览：文章总数、近 24h、覆盖省份/来源数、来源健康分布。

    近 24 小时用 ``published_at_utc`` 与 ``UTC_TIMESTAMP()`` 比较，
    **不依赖服务器时区设置**——本机 MySQL 生产/开发实例常被配成 ``+00:00``，
    用 ``NOW()`` 会与北京时间字段差 8 小时。
    """
    db = db or Database()
    row = _fetch_one(
        db,
        "SELECT "
        " (SELECT COUNT(*) FROM articles) AS articles, "
        " (SELECT COUNT(*) FROM articles "
        "  WHERE published_at_utc >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)) AS articles_24h, "
        " (SELECT COUNT(DISTINCT province_code) FROM sources "
        "  WHERE province_code IS NOT NULL) AS provinces_covered, "
        " (SELECT COUNT(*) FROM sources) AS sources_total, "
        " (SELECT COUNT(*) FROM sources WHERE enabled=1) AS sources_enabled, "
        " (SELECT COUNT(*) FROM crawl_jobs) AS jobs_total, "
        " (SELECT COUNT(*) FROM crawl_jobs WHERE status='succeeded') AS jobs_succeeded, "
        " (SELECT COUNT(*) FROM crawl_jobs WHERE status IN ('failed','retry_wait')) AS jobs_unhealthy",
    )
    total_provinces = 34
    row["provinces_total"] = total_provinces
    row["provinces_missing"] = max(0, total_provinces - int(row.get("provinces_covered") or 0))
    jobs_total = int(row.get("jobs_total") or 0)
    row["success_rate"] = (
        round(100.0 * int(row.get("jobs_succeeded") or 0) / jobs_total, 2) if jobs_total else 0.0
    )
    return row


def by_province(db: Database | None = None, *, limit: int = 40) -> list[dict[str, Any]]:
    """按省份统计文章数与来源数（覆盖 34 个省级行政区）。"""
    db = db or Database()
    return _fetch_all(
        db,
        "SELECT p.code AS province_code, p.name AS province_name, p.region, "
        " COALESCE(src.source_count, 0) AS source_count, "
        " COALESCE(art.article_count, 0) AS article_count, "
        " COALESCE(art.empty_body, 0) AS empty_body, "
        " art.last_published_at "
        "FROM provinces p "
        "LEFT JOIN (SELECT province_code, COUNT(*) AS source_count FROM sources "
        "           GROUP BY province_code) src ON src.province_code = p.code "
        "LEFT JOIN (SELECT s.province_code, COUNT(*) AS article_count, "
        "                  SUM(CASE WHEN a.body_text IS NULL OR a.body_text='' "
        "                      THEN 1 ELSE 0 END) AS empty_body, "
        "                  MAX(a.published_at_cst) AS last_published_at "
        "           FROM articles a JOIN sources s ON s.id = a.source_id "
        "           GROUP BY s.province_code) art ON art.province_code = p.code "
        "ORDER BY article_count DESC, p.code LIMIT %s",
        [limit],
    )


def by_source(db: Database | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    """按来源统计产出与质量。"""
    db = db or Database()
    return _fetch_all(
        db,
        "SELECT s.id, s.adapter_name, s.name, s.province_code, s.category, "
        " s.priority, s.crawl_interval_hours, s.enabled, "
        " COUNT(a.id) AS article_count, "
        " SUM(CASE WHEN a.title IS NULL OR a.title='' THEN 1 ELSE 0 END) AS empty_title, "
        " SUM(CASE WHEN a.body_text IS NULL OR a.body_text='' THEN 1 ELSE 0 END) AS empty_body, "
        " SUM(CASE WHEN a.source_name IS NULL OR a.source_name='' THEN 1 ELSE 0 END) AS empty_source, "
        " SUM(CASE WHEN a.published_at_cst IS NULL THEN 1 ELSE 0 END) AS no_time, "
        " MAX(a.published_at_cst) AS last_published_at "
        "FROM sources s LEFT JOIN articles a ON a.source_id = s.id "
        "GROUP BY s.id ORDER BY article_count DESC LIMIT %s",
        [limit],
    )


def quality(db: Database | None = None) -> dict[str, Any]:
    """字段质量：五类字段的完整率。"""
    db = db or Database()
    row = _fetch_one(
        db,
        "SELECT COUNT(*) AS total, "
        " SUM(CASE WHEN title IS NULL OR title='' THEN 1 ELSE 0 END) AS empty_title, "
        " SUM(CASE WHEN body_text IS NULL OR body_text='' THEN 1 ELSE 0 END) AS empty_body, "
        " SUM(CASE WHEN source_name IS NULL OR source_name='' THEN 1 ELSE 0 END) AS empty_source, "
        " SUM(CASE WHEN published_at_cst IS NULL THEN 1 ELSE 0 END) AS no_time, "
        " SUM(CASE WHEN url IS NULL OR url='' THEN 1 ELSE 0 END) AS empty_url "
        "FROM articles",
    )
    total = int(row.get("total") or 0)
    for key in ("title", "body", "source", "time", "url"):
        missing = int(row.get(f"empty_{key}") or 0) if key != "time" else int(row.get("no_time") or 0)
        row[f"{key}_fill_rate"] = (
            round(100.0 * (total - missing) / total, 2) if total else 0.0
        )
    return row


def job_health(db: Database | None = None) -> list[dict[str, Any]]:
    """任务健康度：按状态聚合 + 平均产出。"""
    db = db or Database()
    return _fetch_all(
        db,
        "SELECT status, COUNT(*) AS n, "
        " ROUND(AVG(discovered_count), 1) AS avg_discovered, "
        " ROUND(AVG(saved_count), 1) AS avg_saved, "
        " MAX(finished_at_utc) AS last_finished "
        "FROM crawl_jobs GROUP BY status ORDER BY n DESC",
    )


# --------------------------------------------------------------------------
def render_text(data: Any, title: str = "") -> str:
    """把统计结果渲染成等宽文本表（CLI 用，不引第三方表格库）。"""
    if title:
        pass
    if isinstance(data, dict):
        return _render_dict(data, title)
    if isinstance(data, list) and data:
        return _render_rows(data, title)
    return "（无数据）"


def _render_dict(row: dict[str, Any], title: str) -> str:
    lines = [f"== {title} ==" if title else "=="]
    width = max((len(str(k)) for k in row), default=0)
    for key, value in row.items():
        lines.append(f"  {str(key):<{width}} : {value}")
    return "\n".join(lines)


def _render_rows(rows: list[dict[str, Any]], title: str) -> str:
    headers = list(rows[0].keys())
    widths = {
        h: max(len(str(h)), max(len(_fmt(r.get(h))) for r in rows)) for h in headers
    }
    lines = [f"== {title} ==" if title else "=="]
    lines.append("  " + "  ".join(str(h).ljust(widths[h]) for h in headers))
    lines.append("  " + "  ".join("-" * widths[h] for h in headers))
    for row in rows:
        lines.append("  " + "  ".join(_fmt(row.get(h)).ljust(widths[h]) for h in headers))
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
