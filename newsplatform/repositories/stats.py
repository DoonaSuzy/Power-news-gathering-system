"""统计仓储（对应改造方案 §3.6 的 ``/api/v1/stats/*`` 与 §九.5「采集质量看板」）。

口径与采集层 ``newsagg/stats.py`` 对齐，避免「CLI 看到的数字和 API 给的数字不一致」。
近 24 小时一律用 ``published_at_utc`` 与 ``utc_timestamp()`` 配对比较——
不依赖 MySQL 服务器时区（本机实例是 ``default-time-zone=+00:00``，
用 ``NOW()`` 去比北京时间字段会整体差 8 小时）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.models.orm import (
    Article,
    ArticleTask,
    CrawlJob,
    CrawlRun,
    Province,
    Source,
)
from newsplatform.repositories.base import BaseRepository

__all__ = ["StatsRepository"]

#: 省级行政区总数（34 = 31 大陆 + 港澳台）
TOTAL_PROVINCES = 34


class StatsRepository(BaseRepository):
    """聚合查询。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ---------------- 概览 ----------------
    async def overview(self) -> dict[str, Any]:
        """全局概览：文章总量、近 24h、覆盖率、任务健康度。"""
        articles = int((await self.session.execute(
            select(func.count()).select_from(Article.__table__)
        )).scalar() or 0)

        # MySQL 的 DATE_SUB(..., INTERVAL 1 DAY)；INTERVAL 字面量无法用绑定参数表达，
        # 这里用 text() 直接拼（常量，无注入面）
        day_ago = func.date_sub(func.utc_timestamp(), text("INTERVAL 1 DAY"))
        articles_24h = int((await self.session.execute(
            select(func.count()).select_from(Article.__table__).where(
                Article.published_at_utc >= day_ago
            )
        )).scalar() or 0)

        provinces_covered = int((await self.session.execute(
            select(func.count(func.distinct(Source.province_code))).where(
                Source.province_code.is_not(None)
            )
        )).scalar() or 0)
        sources_total = int((await self.session.execute(
            select(func.count()).select_from(Source.__table__)
        )).scalar() or 0)
        sources_enabled = int((await self.session.execute(
            select(func.count()).select_from(Source.__table__).where(Source.enabled.is_(True))
        )).scalar() or 0)
        jobs_total = int((await self.session.execute(
            select(func.count()).select_from(CrawlJob.__table__)
        )).scalar() or 0)
        jobs_succeeded = int((await self.session.execute(
            select(func.count()).select_from(CrawlJob.__table__).where(
                CrawlJob.status == "succeeded"
            )
        )).scalar() or 0)
        jobs_unhealthy = int((await self.session.execute(
            select(func.count()).select_from(CrawlJob.__table__).where(
                CrawlJob.status.in_(("failed", "retry_wait"))
            )
        )).scalar() or 0)

        return {
            "articles": articles,
            "articles_24h": articles_24h,
            "provinces_covered": provinces_covered,
            "provinces_total": TOTAL_PROVINCES,
            "provinces_missing": max(0, TOTAL_PROVINCES - provinces_covered),
            "sources_total": sources_total,
            "sources_enabled": sources_enabled,
            "jobs_total": jobs_total,
            "jobs_succeeded": jobs_succeeded,
            "jobs_unhealthy": jobs_unhealthy,
            "success_rate": (
                round(100.0 * jobs_succeeded / jobs_total, 2) if jobs_total else 0.0
            ),
        }

    # ---------------- 字段质量 ----------------
    async def quality(self) -> dict[str, Any]:
        """五类字段完整率（标题 / 正文 / 发布时间 / 来源 / 链接）。"""
        row = (await self.session.execute(
            select(
                func.count().label("total"),
                func.sum(case((Article.title == "", 1), else_=0)).label("empty_title"),
                func.sum(
                    case((Article.body_text.is_(None) | (Article.body_text == ""), 1), else_=0)
                ).label("empty_body"),
                func.sum(case((Article.source_name == "", 1), else_=0)).label("empty_source"),
                func.sum(case((Article.published_at_cst.is_(None), 1), else_=0)).label("no_time"),
                func.sum(case((Article.url == "", 1), else_=0)).label("empty_url"),
            ).select_from(Article.__table__)
        )).mappings().first() or {}

        total = int(row.get("total") or 0)
        result: dict[str, Any] = {
            "total": total,
            "empty_title": int(row.get("empty_title") or 0),
            "empty_body": int(row.get("empty_body") or 0),
            "empty_source": int(row.get("empty_source") or 0),
            "no_time": int(row.get("no_time") or 0),
            "empty_url": int(row.get("empty_url") or 0),
        }
        for key, missing in (
            ("title", result["empty_title"]),
            ("body", result["empty_body"]),
            ("source", result["empty_source"]),
            ("time", result["no_time"]),
            ("url", result["empty_url"]),
        ):
            result[f"{key}_fill_rate"] = (
                round(100.0 * (total - missing) / total, 2) if total else 0.0
            )
        return result

    # ---------------- 按来源 ----------------
    async def by_source(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """按来源统计产出与字段缺漏。"""
        stmt = (
            select(
                Source.id,
                Source.adapter_name,
                Source.name,
                Source.province_code,
                Source.category,
                Source.priority,
                Source.crawl_interval_hours,
                Source.enabled,
                func.count(Article.id).label("article_count"),
                func.sum(case((Article.title == "", 1), else_=0)).label("empty_title"),
                func.sum(
                    case((Article.body_text.is_(None) | (Article.body_text == ""), 1), else_=0)
                ).label("empty_body"),
                func.sum(case((Article.published_at_cst.is_(None), 1), else_=0)).label("no_time"),
                func.max(Article.published_at_cst).label("last_published_at"),
            )
            .select_from(Source.__table__.join(Article.__table__, Article.source_id == Source.id, isouter=True))
            .group_by(Source.id)
            .order_by(func.count(Article.id).desc())
            .limit(limit)
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    # ---------------- 任务 ----------------
    async def job_health(self) -> list[dict[str, Any]]:
        """按状态聚合的任务健康度。"""
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
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """最近任务列表（运维排查用）。"""
        stmt = (
            select(
                CrawlJob.id,
                CrawlJob.source_id,
                Source.adapter_name,
                Source.province_code,
                CrawlJob.job_type,
                CrawlJob.status,
                CrawlJob.window_start_cst,
                CrawlJob.window_end_cst,
                CrawlJob.discovered_count,
                CrawlJob.saved_count,
                CrawlJob.request_count,
                CrawlJob.stop_reason,
                CrawlJob.started_at_utc,
                CrawlJob.finished_at_utc,
            )
            .join(Source.__table__, Source.id == CrawlJob.source_id, isouter=True)
            .order_by(CrawlJob.id.desc())
            .limit(limit)
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    async def recent_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """最近协调器批次。"""
        stmt = (
            select(
                CrawlRun.id,
                CrawlRun.trigger_key,
                CrawlRun.status,
                CrawlRun.job_count,
                CrawlRun.created_at,
                CrawlRun.finished_at_utc,
            )
            .order_by(CrawlRun.id.desc())
            .limit(limit)
        )
        return [dict(r) for r in (await self.session.execute(stmt)).mappings().all()]

    # ---------------- 候选台账 ----------------
    async def task_funnel(self) -> dict[str, int]:
        """候选漏斗：发现 → 成功 → 跳过 → 失败。"""
        stmt = (
            select(ArticleTask.status, func.count().label("n"))
            .group_by(ArticleTask.status)
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        funnel = {str(r["status"]): int(r["n"]) for r in rows}
        funnel["total"] = sum(funnel.values())
        return funnel

    async def province_dict(self) -> dict[str, str]:
        """省份代码 → 名称（服务层拼装展示字段用）。"""
        rows = (await self.session.execute(
            select(Province.code, Province.name)
        )).mappings().all()
        return {str(r["code"]): str(r["name"]) for r in rows}
