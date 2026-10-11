"""抓取引擎：取租约 → 开会话 → 跑蜘蛛 → 写终态。

调用关系::

    worker.py  ──subprocess──▶  python -m newsagg run-job <job_id>
                                     │
                                     ▼
                              engine.run_blocking()
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
              open_session()   SiteSpider(...)   conclude()

引擎不写业务 SQL（委托 session/repo），只负责编排生命周期。
每个任务独占一个进程：Twisted 反应器不可在同一进程内重启，
同一进程内重复 ``CrawlerProcess`` 会报 ReactorNotRestartable。
"""

from __future__ import annotations

from newsagg.config import get_settings
from newsagg.crawl.session import CrawlSession
from newsagg.crawl.settings import scrapy_settings
from newsagg.crawl.spider import SiteSpider
from newsagg.db import Database
from newsagg.errors import JobNotFound
from newsagg.logutil import bind_context, get_logger
from newsagg.queue import StreamQueue
from newsagg.repo import Repository

log = get_logger("engine")

__all__ = [
    "worker_identity",
    "open_session",
    "run_blocking",
    "conclude",
    "runner",
]


def worker_identity() -> str:
    """当前 worker 身份：主机名+PID（``POWERNEWS_OWNER`` 可覆盖）。"""
    import os
    import socket

    override = os.environ.get("POWERNEWS_OWNER")
    if override:
        return override
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_bloom():
    """构造 URL 布隆过滤器；Redis 不可用时返回 None（退化为纯库内去重）。"""
    from newsagg.crawl.dedup import UrlBloom

    try:
        return UrlBloom(StreamQueue().client)
    except Exception as exc:  # noqa: BLE001
        log.warning("布隆过滤器不可用，退化为库内去重: %s", exc)
        return None


def open_session(
    job_id: int,
    *,
    db: Database | None = None,
    owner: str | None = None,
    bloom=None,
    refresh: bool = False,
) -> CrawlSession:
    """抢占租约并构造会话。终态 / 被他人持有时抛对应异常。"""
    settings = get_settings()
    db = db or Database(settings)
    repo = Repository(db)
    owner = owner or worker_identity()
    bloom = _build_bloom() if bloom is None else bloom

    conn = db.connect()
    try:
        job_row, lease = repo.acquire_lease(
            conn, job_id, owner, lease_seconds=settings.lease_seconds
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    conn = db.connect()
    try:
        meta = repo.get_job_source_meta(conn, job_id)
    finally:
        conn.close()
    if not meta:
        raise JobNotFound(f"任务不存在: {job_id}")

    from newsagg.timewindow import Window

    window = Window(meta["window_start_cst"], meta["window_end_cst"], meta["job_type"])
    return CrawlSession(
        job_id,
        source_id=int(meta["id"]),
        window=window,
        adapter_name=meta["adapter_name"],
        lease_token=lease["lease_token"],
        owner=owner,
        db=db,
        repo=repo,
        bloom=bloom,
        refresh=refresh,
    )


def _list_url(session: CrawlSession) -> str:
    """取来源栏目页 URL（优先库内 list_url，回退适配器默认）。"""
    conn = session.db.connect()
    try:
        source = session.repo.get_source(conn, source_id=session.source_id)
    finally:
        conn.close()
    if source and source.list_url:
        return source.list_url

    from newsagg.adapters import ensure_loaded, get_adapter

    ensure_loaded()
    adapter = get_adapter(session.adapter_name)
    return adapter.column_url or adapter.list_url


def _adapter_overrides(session: CrawlSession) -> dict:
    """按来源下发 Scrapy 设置覆盖（目前只有 TLS 套件）。

    少数老站只接受低安全等级套件（``DEFAULT:@SECLEVEL=1``），Scrapy 不支持
    逐请求设置 TLS 套件，因此只能在**该来源独占的任务进程**里下发；
    未声明的来源保持 OpenSSL 默认套件。
    """
    from newsagg.adapters import ensure_loaded, get_adapter

    ensure_loaded()
    try:
        adapter = get_adapter(session.adapter_name)
    except KeyError:
        return {}

    overrides: dict = {}
    ciphers = getattr(adapter, "tls_ciphers", None)
    if ciphers:
        overrides["DOWNLOADER_CLIENT_TLS_CIPHERS"] = ciphers
    robots_policy = getattr(adapter, "robots_policy", None)
    if robots_policy:
        overrides["POWERNEWS_ROBOTS_OVERRIDES"] = {session.adapter_name: robots_policy}
    return overrides


def runner(**overrides):
    """返回配置好的 ``CrawlerRunner``（供测试或自定义编排使用）。"""
    from scrapy.crawler import CrawlerRunner

    return CrawlerRunner(scrapy_settings(**overrides))


def run_blocking(
    job_id: int,
    *,
    db: Database | None = None,
    owner: str | None = None,
    refresh: bool = False,
) -> dict:
    """同步跑一个任务并等待完成，写终态后返回统计。

    队列 XACK 由调用方（父进程 worker）在子进程写终态成功后统一执行。
    """
    from scrapy.crawler import CrawlerProcess

    session = open_session(job_id, db=db, owner=owner, refresh=refresh)
    list_url = _list_url(session)
    bind_context(job_id=job_id, adapter=session.adapter_name)

    process = CrawlerProcess(scrapy_settings(**_adapter_overrides(session)))

    session.start_renewal()
    try:
        # Scrapy 2.13+ 的 crawl() 只接受蜘蛛「类」+ 构造参数，不接受实例
        process.crawl(SiteSpider, session=session, list_url=list_url)
        process.start(stop_after_crawl=True)
    finally:
        session.stop_renewal()

    return conclude(session)


def conclude(session: CrawlSession) -> dict:
    """写终态并返回统计。XACK 由调用方负责。"""
    status, retry_minutes = session.decide()
    if status == "retry_wait":
        session.fail_task(retry_minutes)
    else:
        session.finish(status)

    stats = {
        "job_id": session.job_id,
        "status": status,
        "discovered": session.discovered,
        "saved": session.saved,
        "requests": session.requests,
        "stop_reason": session.stop_reason,
    }
    log.info("job %s 完成: %s", session.job_id, stats)
    return stats
