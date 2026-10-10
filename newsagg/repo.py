"""持久化门面：**全部 SQL 的唯一落点**。

业务层（worker / spider / session / cli / schedule）不得直接书写 SQL，
一律通过本模块。这样做的收益：

1. 数据库结构变更只需改一处；
2. 单元测试可用假连接验证 SQL 契约（见 ``tests/test_repo.py``）；
3. 便于后续接入 Alembic 迁移（改造方案 §6.2）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from newsagg.db import Database

__all__ = [
    "SourceRow",
    "JobRow",
    "ArticleUpsert",
    "Repository",
    "TASK_STATUSES",
]

TASK_STATUSES = ("pending", "done", "skipped", "failed")


@dataclass(frozen=True, slots=True)
class SourceRow:
    """来源快照。"""

    id: int
    adapter_name: str
    name: str
    province_code: str | None
    base_url: str
    list_url: str
    category: str
    priority: int
    crawl_interval_hours: int
    enabled: bool
    status: str


@dataclass(frozen=True, slots=True)
class JobRow:
    """采集任务快照。"""

    id: int
    source_id: int
    job_type: str
    status: str
    window_start_cst: datetime
    window_end_cst: datetime
    lease_token: str | None = None
    lease_owner: str | None = None
    lease_until_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArticleUpsert:
    """文章 upsert 结果。"""

    outcome: str  # saved | updated | skipped
    article_id: int | None = None


class Repository:
    """围绕 :class:`Database` 的持久化门面。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ================= 来源 =================
    _SOURCE_COLS = (
        "s.id, s.adapter_name, s.name, s.province_code, s.base_url, s.list_url, "
        "s.category, s.priority, s.crawl_interval_hours, s.enabled, s.status"
    )

    @staticmethod
    def _to_source(row: dict[str, Any] | None) -> SourceRow | None:
        if not row:
            return None
        return SourceRow(
            id=int(row["id"]),
            adapter_name=row.get("adapter_name") or "",
            name=row.get("name") or "",
            province_code=row.get("province_code"),
            base_url=row.get("base_url") or "",
            list_url=row.get("list_url") or "",
            category=row.get("category") or "",
            priority=int(row.get("priority") or 0),
            crawl_interval_hours=int(row.get("crawl_interval_hours") or 1),
            enabled=bool(row.get("enabled")),
            status=row.get("status") or "active",
        )

    def get_source(self, conn, *, source_id: int | None = None,
                   adapter_name: str | None = None) -> SourceRow | None:
        """按 id 或 adapter_name 取来源。"""
        with conn.cursor() as cur:
            if source_id is not None:
                cur.execute(
                    f"SELECT {self._SOURCE_COLS} FROM sources s WHERE s.id=%s", (source_id,)
                )
            elif adapter_name:
                cur.execute(
                    f"SELECT {self._SOURCE_COLS} FROM sources s WHERE s.adapter_name=%s",
                    (adapter_name,),
                )
            else:
                return None
            return self._to_source(cur.fetchone())

    def list_sources(self, conn, *, category: str | None = None,
                     province_code: str | None = None,
                     enabled_only: bool = False) -> list[SourceRow]:
        """列出来源（可按类别 / 省份 / 启用状态筛选）。"""
        sql = f"SELECT {self._SOURCE_COLS} FROM sources s WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND s.category=%s"
            params.append(category)
        if province_code:
            sql += " AND s.province_code=%s"
            params.append(province_code)
        if enabled_only:
            sql += " AND s.enabled=1"
        sql += " ORDER BY s.priority, s.id"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r for r in (self._to_source(x) for x in cur.fetchall()) if r]

    def list_enabled_slots(self, conn) -> list[dict[str, Any]]:
        """列出省份槽位（一省一启用槽）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sl.province_code, sl.adapter_name, s.id AS source_id "
                "FROM source_slots sl LEFT JOIN sources s ON s.adapter_name=sl.adapter_name "
                "ORDER BY sl.province_code"
            )
            return list(cur.fetchall())

    def set_source_enabled(self, conn, adapter_name: str, enabled: bool) -> bool:
        """启用 / 停用来源。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET enabled=%s WHERE adapter_name=%s",
                (1 if enabled else 0, adapter_name),
            )
            return cur.rowcount > 0

    def mark_discovery_success(self, conn, source_id: int, at_utc: datetime) -> None:
        """记录该来源最近一次列表发现成功时间（增量窗口依据）。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET last_discovery_success_at_utc=%s WHERE id=%s",
                (at_utc, source_id),
            )

    def set_cooldown(self, conn, source_id: int, until_utc: datetime) -> None:
        """设置封禁冷却到期时间。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET cooldown_until_utc=%s WHERE id=%s",
                (until_utc, source_id),
            )

    # ================= 批次与任务 =================
    def create_run(self, conn, trigger_key: str) -> int | None:
        """创建批次（幂等）。已存在返回 None。"""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO crawl_runs (trigger_key, status) VALUES (%s,'running')",
                (trigger_key,),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    def finish_run(self, conn, run_id: int, job_count: int) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crawl_runs SET status='finished', job_count=%s, "
                "finished_at_utc=UTC_TIMESTAMP() WHERE id=%s",
                (job_count, run_id),
            )

    def create_job(
        self, conn, *, source_id: int, trigger_key: str, job_type: str,
        window_start_cst: datetime, window_end_cst: datetime, priority: int = 0,
    ) -> int | None:
        """创建采集任务（幂等，靠唯一键）。已存在返回 None。"""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO crawl_jobs "
                "(source_id, trigger_key, job_type, status, priority, "
                " window_start_cst, window_end_cst) "
                "VALUES (%s,%s,%s,'queued',%s,%s,%s)",
                (source_id, trigger_key, job_type, priority,
                 window_start_cst, window_end_cst),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    @staticmethod
    def _to_job(row: dict[str, Any] | None) -> JobRow | None:
        if not row:
            return None
        return JobRow(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            job_type=row.get("job_type") or "",
            status=row.get("status") or "",
            window_start_cst=row["window_start_cst"],
            window_end_cst=row["window_end_cst"],
            lease_token=row.get("lease_token"),
            lease_owner=row.get("lease_owner"),
            lease_until_utc=row.get("lease_until_utc"),
        )

    def find_open_job(self, conn, job_id: int) -> JobRow | None:
        """读取任务快照（含来源租约字段）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.id, j.source_id, j.job_type, j.status, "
                "j.window_start_cst, j.window_end_cst, "
                "s.lease_token, s.lease_owner, s.lease_until_utc "
                "FROM crawl_jobs j JOIN sources s ON s.id=j.source_id "
                "WHERE j.id=%s",
                (job_id,),
            )
            return self._to_job(cur.fetchone())

    def get_job_source_meta(self, conn, job_id: int) -> dict[str, Any] | None:
        """取任务的来源元信息（id / adapter_name / province_code / list_url）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.id, s.adapter_name, s.province_code, s.name, s.list_url, "
                "j.window_start_cst, j.window_end_cst, j.job_type "
                "FROM crawl_jobs j JOIN sources s ON s.id=j.source_id "
                "WHERE j.id=%s",
                (job_id,),
            )
            return cur.fetchone()

    def acquire_lease(self, conn, job_id: int, owner: str, *,
                      lease_seconds: int = 900) -> tuple[dict[str, Any], Any]:
        """抢占任务租约（行锁 + 幂等）。

        返回 ``(任务行, 租约)``；已被他人持有时抛 ``LeaseBusy``。
        """
        from newsagg.errors import JobNotFound, LeaseBusy

        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.id, j.source_id, j.status, j.window_start_cst, j.window_end_cst, "
                "j.job_type, s.lease_token, s.lease_owner, s.lease_until_utc "
                "FROM crawl_jobs j JOIN sources s ON s.id=j.source_id "
                "WHERE j.id=%s FOR UPDATE",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise JobNotFound(f"任务不存在: {job_id}")
            if row["status"] in ("succeeded", "failed", "skipped"):
                raise LeaseBusy(f"任务已终态: {row['status']}")

            holder = row.get("lease_owner")
            until = row.get("lease_until_utc")
            cur.execute("SELECT UTC_TIMESTAMP() AS now_utc")
            now_utc = (cur.fetchone() or {}).get("now_utc")
            if holder and holder != owner and until and now_utc and until > now_utc:
                raise LeaseBusy(f"任务被 {holder} 持有至 {until}")

            cur.execute(
                "UPDATE sources SET lease_token=UUID(), lease_owner=%s, "
                "lease_until_utc=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) WHERE id=%s",
                (owner, lease_seconds, row["source_id"]),
            )
            cur.execute(
                "SELECT lease_token, lease_owner, lease_until_utc FROM sources WHERE id=%s",
                (row["source_id"],),
            )
            lease = cur.fetchone()

            cur.execute(
                "UPDATE crawl_jobs SET status='running', started_at_utc=COALESCE("
                "started_at_utc, UTC_TIMESTAMP()) WHERE id=%s",
                (job_id,),
            )
            return row, lease

    def renew_lease(self, conn, *, source_id: int, token: str,
                    lease_seconds: int = 900) -> bool:
        """续租。返回 False 表示租约已失效（调用方应立即停止）。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET lease_until_utc="
                "DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) "
                "WHERE id=%s AND lease_token=%s",
                (lease_seconds, source_id, token),
            )
            return cur.rowcount > 0

    def release_lease(self, conn, *, source_id: int, token: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET lease_token=NULL, lease_owner=NULL, "
                "lease_until_utc=NULL WHERE id=%s AND lease_token=%s",
                (source_id, token),
            )

    def finish_job(self, conn, *, job_id: int, status: str, discovered: int,
                   saved: int, requests: int, stop_reason: str) -> None:
        """写任务终态。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crawl_jobs SET status=%s, discovered_count=%s, saved_count=%s, "
                "request_count=%s, stop_reason=%s, finished_at_utc=UTC_TIMESTAMP() "
                "WHERE id=%s",
                (status, discovered, saved, requests, stop_reason, job_id),
            )

    def set_retry(self, conn, *, job_id: int, retry_minutes: int, reason: str) -> None:
        """置为 retry_wait，N 分钟后可重试。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crawl_jobs SET status='retry_wait', stop_reason=%s, "
                "retry_at_utc=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE) WHERE id=%s",
                (reason, retry_minutes, job_id),
            )

    # ================= 候选与文章 =================
    def upsert_discovered(self, conn, *, source_id: int, job_id: int,
                          url_hash: str, url: str, list_title: str = "") -> bool:
        """插入候选（幂等）。首次插入返回 True。

        已存在时若本次带了 ``list_title`` 而库内为空，顺手回填——列表页标题
        是详情页标题缺失时的兜底来源。
        """
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO article_tasks "
                "(source_id, job_id, discovered_url_hash, url, list_title) "
                "VALUES (%s,%s,%s,%s,%s)",
                (source_id, job_id, url_hash, url, list_title or None),
            )
            if cur.rowcount > 0:
                return True
            if list_title:
                cur.execute(
                    "UPDATE article_tasks SET list_title=%s "
                    "WHERE source_id=%s AND discovered_url_hash=%s "
                    "AND (list_title IS NULL OR list_title='')",
                    (list_title, source_id, url_hash),
                )
            return False

    def mark_task(self, conn, *, source_id: int, url_hash: str, status: str,
                  failure_class: str = "", skip_reason: str = "") -> None:
        """更新候选处理状态。"""
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE article_tasks SET status=%s, failure_class=%s, skip_reason=%s, "
                "attempts=attempts+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE source_id=%s AND discovered_url_hash=%s",
                (status, failure_class or None, skip_reason or None, source_id, url_hash),
            )

    def save_article(
        self, conn, *, source_id: int, url_hash: str, url: str, title: str,
        body_text: str, source_name: str = "",
        published_utc: datetime | None = None, published_cst: datetime | None = None,
        time_precision: str = "unknown", content_hash: str = "",
        quality_flags: list[str] | None = None,
    ) -> ArticleUpsert:
        """upsert 文章。身份 = ``(source_id, url_hash)``。

        正文未变（``content_hash`` 相同）时不整行跳过：标题、来源、发布时间、
        精度、质量标记是独立列，抽取规则改进后应据此修复旧记录。仅更新确有变化的列。
        """
        flags = json.dumps(quality_flags or [], ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content_hash, title, source_name, published_at_utc, "
                "published_at_cst, time_precision, quality_flags "
                "FROM articles WHERE source_id=%s AND url_hash=%s",
                (source_id, url_hash),
            )
            existing = cur.fetchone()

            if existing:
                body_same = existing.get("content_hash") == content_hash

                def col(key: str) -> str:
                    value = existing.get(key)
                    return value if isinstance(value, str) else ("" if value is None else str(value))

                meta: dict[str, Any] = {}
                if col("title") != (title or ""):
                    meta["title"] = title or ""
                if col("source_name") != (source_name or ""):
                    meta["source_name"] = source_name or ""
                if existing.get("published_at_utc") != published_utc:
                    meta["published_at_utc"] = published_utc
                if existing.get("published_at_cst") != published_cst:
                    meta["published_at_cst"] = published_cst
                if col("time_precision") != (time_precision or "unknown"):
                    meta["time_precision"] = time_precision or "unknown"
                if col("quality_flags") != flags:
                    meta["quality_flags"] = flags

                if body_same and not meta:
                    return ArticleUpsert("skipped", int(existing["id"]))

                if body_same:
                    assignments = ", ".join(f"{k}=%s" for k in meta)
                    cur.execute(
                        f"UPDATE articles SET {assignments}, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=%s",
                        (*meta.values(), existing["id"]),
                    )
                    return ArticleUpsert("updated", int(existing["id"]))

                cur.execute(
                    "UPDATE articles SET url=%s, title=%s, body_text=%s, source_name=%s, "
                    "published_at_utc=%s, published_at_cst=%s, time_precision=%s, "
                    "content_hash=%s, quality_flags=%s, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=%s",
                    (url, title, body_text, source_name, published_utc, published_cst,
                     time_precision, content_hash, flags, existing["id"]),
                )
                return ArticleUpsert("updated", int(existing["id"]))

            # 跨来源同文检测：正文指纹命中其他来源首发 → 打标记
            if content_hash:
                cur.execute(
                    "SELECT source_id FROM articles WHERE content_hash=%s LIMIT 1",
                    (content_hash,),
                )
                first = cur.fetchone()
                if first and int(first["source_id"]) != source_id:
                    cur.execute(
                        "SELECT province_code FROM sources WHERE id=%s",
                        (int(first["source_id"]),),
                    )
                    prow = cur.fetchone() or {}
                    origin = prow.get("province_code") or str(first["source_id"])
                    marker = f"duplicate_of:{origin}"
                    if marker not in flags:
                        flags = json.dumps(
                            json.loads(flags) + [marker], ensure_ascii=False
                        )

            cur.execute(
                "INSERT INTO articles "
                "(source_id, url_hash, url, title, body_text, source_name, "
                " published_at_utc, published_at_cst, time_precision, content_hash, "
                " quality_flags) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (source_id, url_hash, url, title, body_text, source_name,
                 published_utc, published_cst, time_precision, content_hash, flags),
            )
            return ArticleUpsert("saved", int(cur.lastrowid))

    def get_task(self, conn, *, source_id: int, url_hash: str) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM article_tasks WHERE source_id=%s AND discovered_url_hash=%s",
                (source_id, url_hash),
            )
            return cur.fetchone()
