"""采集会话：一次站点任务的全部运行期状态。

职责边界（与 ``engine`` / ``spider`` 的分工）：

- ``engine`` 负责生命周期编排（取租约 → 跑蜘蛛 → 写终态）；
- ``spider`` 负责「抓 + 解析」，不直接写库；
- ``session``（本模块）负责**一切写库与状态判定**：候选登记、去重、
  窗口判定、正文落库、租约续期、停机决策。

这样蜘蛛里没有一句 SQL，测试时可用假 session 驱动。
"""

from __future__ import annotations

import threading
from typing import Any

from newsagg.cleaning import now_utc_naive
from newsagg.config import get_settings
from newsagg.crawl.netpolicy import BLOCK_KINDS
from newsagg.db import Database
from newsagg.logutil import get_logger
from newsagg.repo import Repository
from newsagg.timewindow import Window

log = get_logger("session")

__all__ = ["CrawlSession", "BLOCK_HTTP_CODES"]

#: 视为「被封禁」的状态码，命中后停机并进入冷却
BLOCK_HTTP_CODES = {403, 405, 412, 429}

#: 触发停机的失败比例阈值
FAILURE_RATIO_LIMIT = 0.6
#: 样本量下限（低于此值不据此停机，避免小样本误判）
FAILURE_MIN_SAMPLES = 20
#: 单任务请求上限
MAX_REQUESTS = 3000


class CrawlSession:
    """一次采集任务的运行期上下文。"""

    def __init__(
        self,
        job_id: int,
        *,
        source_id: int,
        window: Window,
        adapter_name: str = "",
        lease_token: str | None = None,
        owner: str = "",
        db: Database | None = None,
        repo: Repository | None = None,
        bloom: Any = None,
        refresh: bool = False,
    ) -> None:
        self.job_id = job_id
        self.source_id = source_id
        #: 本次任务遇到的阻断种类（river_security / path_or_waf / rate_limit …）。
        #: 只记种类不记次数：判定「要不要重试」看的是种类，不是撞了几次。
        self.block_kinds: set[str] = set()
        self.adapter_name = adapter_name
        self.window = window
        self.lease_token = lease_token
        self.owner = owner
        self.settings = get_settings()
        self.db = db or Database(self.settings)
        self.repo = repo or Repository(self.db)
        self.bloom = bloom
        self.refresh = refresh

        # ---- 运行期计数 ----
        self.discovered = 0
        #: 列表页**解析出的条目数**（无论新旧）。与 ``discovered`` 的区别见
        #: :meth:`decide` 的 docstring —— 判「列表页是否真空」只能看这个。
        self.listed = 0
        self.saved = 0
        self.requests = 0
        self.failures = 0
        self.blocked = 0
        #: 只停止继续翻页（不拦已发出的详情请求）。见 ``stop_paging_only``。
        self.stop_paging = False

        # ---- 停机状态 ----
        self.should_stop = False
        self.stop_reason = ""

        # ---- 列表页标题缓存（详情页标题缺失时兜底）----
        self._list_titles: dict[str, str] = {}

        # ---- 租约续期线程 ----
        self._renew_thread: threading.Thread | None = None
        self._renew_stop = threading.Event()
        self._lease_lost = False

    # ================= 判定 =================
    def budget_hit(self) -> bool:
        """是否超出预算（请求数 / 失败比例）。"""
        if self.requests >= MAX_REQUESTS:
            self.request_stop("request_budget")
            return True
        if (
            self.failures + self.blocked >= FAILURE_MIN_SAMPLES
            and (self.failures + self.blocked) / max(1, self.requests) > FAILURE_RATIO_LIMIT
        ):
            self.request_stop("failure_ratio")
            return True
        if self._lease_lost:
            self.request_stop("lease_lost")
            return True
        return False

    def request_stop(self, reason: str) -> None:
        """标记停机（保留首个原因）。"""
        if not self.stop_reason:
            self.stop_reason = reason
            log.info("job %s 停机: %s", self.job_id, reason)
        self.should_stop = True

    def stop_paging_only(self, reason: str) -> None:
        """只停止**继续翻页**，不停止已发出的请求。

        ⚠️ 这是修一个真实 bug 引入的方法，**不要用 ``request_stop`` 替代**。

        场景：列表页已翻到窗口起点（``window_start_reached``）。
        此时**本页候选的详情请求已经 yield 出去了**（Scrapy 异步排队），
        若用 ``request_stop`` 设了全局 ``should_stop``，
        那些详情请求的回调 ``parse_detail`` 会被开头的 ``_guard()`` **全部丢弃**。

        实测现象：``discovered=26 / requests=1 / saved=0`` ——
        候选登记了、列表页只请求 1 次、详情一条没落库，
        且 ``--refresh`` 无效（它只影响 ``consider()``，管不到 ``_guard()``）。

        所以「翻到窗口边界」只是**翻页终止条件**，不是「任务停机」：
        记 ``stop_reason``（台账要统计"为什么停"），但**不动 ``should_stop``**。
        """
        if not self.stop_reason:
            self.stop_reason = reason
        self.stop_paging = True
        log.info("job %s 停止翻页: %s（已发出的详情请求继续处理）", self.job_id, reason)

    def note_block(self, status: int) -> None:
        """记录一次被封，并记下阻断种类。"""
        if status in BLOCK_HTTP_CODES:
            self.blocked += 1
            kind, _retryable = BLOCK_KINDS.get(status, ("unknown", True))
            self.block_kinds.add(kind)
            if self.blocked >= 5:
                self.request_stop(f"blocked_http_{status}")

    def note_failure(self) -> None:
        self.failures += 1

    def decide(self) -> tuple[str, int]:
        """任务终态判定。返回 ``(status, retry_minutes)``。

        ⚠️ ``listed`` 与 ``discovered`` 必须分开看（这里踩过一个真 bug）：

        * ``listed``     —— 列表页**解析出的条目数**（无论新旧）
        * ``discovered`` —— 其中**本次新发现**的候选数（旧的会被 ``consider()`` 过滤）

        增量采集时，若站点本轮没有更新，``discovered`` 会是 0 但 ``listed`` 正常。
        早期用 ``discovered == 0`` 判断「列表页没拿到候选」，
        导致**站点没更新也被判成异常**，45 个来源里 39 个卡在 ``retry_wait``、
        调度器反复重试。所以「列表页是否真空」只能看 ``listed``。
        """
        # 瑞数（412）这类**挑战型**阻断：重试拿到的还是同一张挑战页，
        # 白白多打几次只会加重风控。直接判失败，由调用方给来源设冷却。
        if "river_security" in self.block_kinds:
            return "failed", 0
        if self.blocked >= 5:
            return "retry_wait", 60
        if self.stop_reason in ("request_budget", "failure_ratio"):
            return "retry_wait", 30
        if self.discovered > 0 and self.saved == 0 and self.failures > 0:
            return "retry_wait", 30
        # 列表页**一条都没解析出来**才是真异常（站点改版 / 路径失效 / 选择器过期）
        if self.listed == 0:
            return "retry_wait", 120
        if self.saved > 0:
            return "partial" if self.should_stop else "succeeded", 0
        # listed > 0 但 saved == 0：本轮无新文章，属正常（站点未更新）
        return "succeeded", 0

    # ================= 列表页 =================
    def consider(self, url: str, url_hash: str, list_title: str = "") -> bool:
        """登记候选。返回 True 表示「需要抓详情」。

        返回 False 的两种情况：库里已有该候选（非修复模式），或布隆已见。
        """
        if list_title:
            self._list_titles[url_hash] = list_title

        conn = self.db.connect()
        try:
            inserted = self.repo.upsert_discovered(
                conn,
                source_id=self.source_id,
                job_id=self.job_id,
                url_hash=url_hash,
                url=url,
                list_title=list_title,
            )
            conn.commit()
        finally:
            conn.close()

        if inserted:
            self.discovered += 1
            if self.bloom is not None:
                try:
                    self.bloom.mark(url_hash)
                except Exception:  # noqa: BLE001
                    pass
            return True

        # 已存在：修复模式下强制重抓
        if self.refresh:
            return True

        if self.bloom is not None:
            try:
                if self.bloom.seen(url_hash):
                    return False
            except Exception:  # noqa: BLE001
                pass
        return False

    def list_title_for(self, url_hash: str) -> str:
        """取列表页标题（详情页标题缺失时的兜底）。"""
        return self._list_titles.get(url_hash, "")

    # ================= 详情页 =================
    def store_detail(
        self, *, url: str, url_hash: str, title: str, paragraphs: list[str],
        source_name: str, published_cst, published_utc, time_precision: str,
        content_hash: str, quality_flags: list[str],
        image_refs: list | None = None,
    ) -> str:
        """落库一篇文章（含配图引用），返回 upsert 结果（saved / updated / skipped）。"""
        conn = self.db.connect()
        try:
            result = self.repo.save_article(
                conn,
                source_id=self.source_id,
                url_hash=url_hash,
                url=url,
                title=title,
                body_text="\n".join(paragraphs),
                source_name=source_name,
                published_utc=published_utc,
                published_cst=published_cst,
                time_precision=time_precision,
                content_hash=content_hash,
                quality_flags=quality_flags,
            )
            # 配图登记：**独立于文章 upsert 的结果** ——
            # 文章可能是 skipped（内容没变），但配图仍可能是新提取的
            # （比如上次解析时图片规则还没上线），所以这里不看 outcome。
            if image_refs and result.article_id:
                try:
                    self.repo.save_article_images(
                        conn, article_id=result.article_id,
                        source_id=self.source_id, refs=image_refs,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 配图失败不能连累文章入库 —— 两者生命周期独立
                    from newsagg.logutil import get_logger as _gl

                    _gl("session").warning("配图登记失败（文章已入库）: %s", exc)
            conn.commit()
        finally:
            conn.close()

        if result.outcome in ("saved", "updated"):
            self.saved += 1
        return result.outcome

    def mark_task(self, url_hash: str, status: str, *,
                  failure_class: str = "", skip_reason: str = "") -> None:
        """更新候选处理状态。"""
        conn = self.db.connect()
        try:
            self.repo.mark_task(
                conn,
                source_id=self.source_id,
                url_hash=url_hash,
                status=status,
                failure_class=failure_class,
                skip_reason=skip_reason,
            )
            if status == "failed":
                self.note_failure()
            conn.commit()
        finally:
            conn.close()

    def mark_discovery_success(self) -> None:
        """记录列表发现成功（增量窗口依据）。"""
        conn = self.db.connect()
        try:
            self.repo.mark_discovery_success(conn, self.source_id, now_utc_naive())
            conn.commit()
        finally:
            conn.close()

    # ================= 终态 =================
    def finish(self, status: str) -> None:
        """写任务终态并释放租约。"""
        conn = self.db.connect()
        try:
            self.repo.finish_job(
                conn,
                job_id=self.job_id,
                status=status,
                discovered=self.discovered,
                saved=self.saved,
                requests=self.requests,
                stop_reason=self.stop_reason or status,
            )
            if self.lease_token:
                self.repo.release_lease(conn, source_id=self.source_id, token=self.lease_token)
            conn.commit()
        finally:
            conn.close()

    def fail_task(self, retry_minutes: int) -> None:
        """置为 retry_wait 并释放租约。"""
        conn = self.db.connect()
        try:
            self.repo.set_retry(
                conn, job_id=self.job_id, retry_minutes=retry_minutes,
                reason=self.stop_reason or "retry",
            )
            if self.lease_token:
                self.repo.release_lease(conn, source_id=self.source_id, token=self.lease_token)
            conn.commit()
        finally:
            conn.close()

    # ================= 租约续期 =================
    def start_renewal(self) -> None:
        """启动后台续租线程（每 ``lease_seconds/3`` 续一次）。"""
        if not self.lease_token:
            return
        interval = max(30, self.settings.lease_seconds // 3)

        def _loop() -> None:
            while not self._renew_stop.wait(interval):
                conn = self.db.connect()
                try:
                    ok = self.repo.renew_lease(
                        conn,
                        source_id=self.source_id,
                        token=self.lease_token or "",
                        lease_seconds=self.settings.lease_seconds,
                    )
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    log.warning("续租异常 job=%s: %s", self.job_id, exc)
                    ok = True
                finally:
                    conn.close()
                if not ok:
                    log.error("job %s 租约已失效，停止产出", self.job_id)
                    self._lease_lost = True
                    self.request_stop("lease_lost")
                    return

        self._renew_thread = threading.Thread(
            target=_loop, name=f"lease-renew-{self.job_id}", daemon=True
        )
        self._renew_thread.start()

    def stop_renewal(self) -> None:
        """停止续租线程。"""
        self._renew_stop.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=5)

    # ================= 概览 =================
    def snapshot(self) -> dict[str, Any]:
        """运行期统计快照。"""
        return {
            "job_id": self.job_id,
            "source_id": self.source_id,
            "adapter": self.adapter_name,
            "discovered": self.discovered,
            "saved": self.saved,
            "requests": self.requests,
            "failures": self.failures,
            "blocked": self.blocked,
            "stop_reason": self.stop_reason,
            "window": self.window.describe(),
        }
