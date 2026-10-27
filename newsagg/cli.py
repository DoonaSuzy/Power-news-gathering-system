"""统一命令行入口（``powernews <子命令>`` 或 ``python -m newsagg <子命令>``）。

命令分组
--------

**初始化与自检**

- ``init-db``     建库 → 执行 ``sql/schema.sql`` → 灌 34 省 + 39 来源 + 省份槽位
- ``check``       配置摘要 + MySQL 连通性 + Redis 连通性 + 来源/适配器一致性
- ``validate``    纯离线校验省字典与来源清单（不连库，CI 可跑）
- ``adapters``    列出已注册适配器并逐条跑契约校验

**采集运行时**

- ``schedule``    跑一轮协调（可 ``--loop`` 常驻）
- ``crawl-once``  手工建批（可绕过采集间隔），可选直接执行
- ``run-job``     同步跑单个任务（worker 调用的就是这个）
- ``worker``      单个 worker 主循环（``--once`` 只跑一轮）
- ``supervisor``  多进程监督 N 个 worker

**数据消费**

- ``stats``       采集统计（概览 / 按省份 / 按来源 / 字段质量 / 任务健康）
- ``sources``     来源清单（可看启用状态）
- ``query``       分页检索文章
- ``show``        查看单篇文章全文
- ``export``      导出 CSV（UTF-8 BOM，Excel 直开）
- ``queue``       Redis 队列概览

**市场行情（结构化时间序列，与新闻并列的第二类数据）**

- ``market-fetch`` 抓取 LNG 现价 / CECI 电煤指数 / 南方区域电力现货，幂等入库
- ``market-stats`` 查看 ``market_prices`` 的覆盖情况与最新数据

**服务**

- ``serve``       起数据中台（``newsplatform``，惰性导入，未安装 FastAPI 时给出提示）

设计约定
--------

- 所有子命令都先 :func:`newsagg.logutil.setup_logging`，日志级别取 ``--verbose`` /
  ``POWERNEWS_LOG_LEVEL``。
- 需要写库的命令用 :func:`_open_db` 拿连接，异常统一转成 ``PowerNewsError`` 提示，
  避免把 PyMySQL 的原始堆栈糊到用户脸上。
- ``--help`` 可用：每个子命令都有 ``help`` 说明。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from newsagg import __version__
from newsagg.config import get_settings
from newsagg.errors import PowerNewsError
from newsagg.logutil import bind_context, get_logger, setup_logging
from newsagg.sources_seed import CATEGORY_LABELS
from newsagg.timefilter import RANGE_KEYS, RANGE_LABELS, describe_range, resolve_range

log = get_logger("cli")

__all__ = ["main", "build_parser"]

# 合法取值取自种子清单/快捷档定义本身，不再抄字面量：抄的那份加类别或加档位时
# 一定会漏（API 侧就漏过 gov，20+ 篇文章在 ?category=gov 上 400）。
_CATEGORY_CHOICES = tuple(CATEGORY_LABELS)
_STATS_CHOICES = ("overview", "province", "source", "quality", "jobs")


# ==========================================================================
# 输出辅助
# ==========================================================================
def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文表头会炸；能重配就重配。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - 已重配或无此 API
            pass


def _out(text: str = "") -> None:
    print(text)


def _print_json(payload: Any) -> None:
    _out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _open_db():
    """构造 :class:`Database`（不建连接，只带配置）。"""
    from newsagg.db import Database

    return Database(get_settings())


def _filters_from_args(args: argparse.Namespace):
    """把 CLI 参数转成 :class:`ArticleFilter`。"""
    from newsagg.query import ArticleFilter

    def _parse_day(value: str | None, *, end: bool = False) -> datetime | None:
        if not value:
            return None
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if end and fmt == "%Y-%m-%d":
                return parsed.replace(hour=23, minute=59, second=59)
            return parsed
        raise PowerNewsError(f"无法解析日期: {value}（示例 2026-10-01 或 2026-10-01 12:30）")

    # 窗口与 API 的 ?range= 走同一个 resolve_range：「近1周」在 CLI 和界面
    # 必须是同一天到同一天，否则导出的 CSV 和看板数字对不上（与 API 侧同样互斥）。
    range_key = getattr(args, "range", None)
    window = resolve_range(range_key)
    if window is not None and (getattr(args, "since", None) or getattr(args, "until", None)):
        raise PowerNewsError(
            "--range 与 --since/--until 二选一；要自定义请用 --range custom 或不传 --range"
        )
    if window is not None:
        date_from, date_to = window
    else:
        date_from = _parse_day(getattr(args, "since", None))
        date_to = _parse_day(getattr(args, "until", None), end=True)

    return ArticleFilter(
        province_code=getattr(args, "province", None),
        category=getattr(args, "category", None),
        adapter_name=getattr(args, "source", None),
        date_from=date_from,
        date_to=date_to,
        keyword=getattr(args, "keyword", None),
        body_only=bool(getattr(args, "body_only", False)),
        exclude_duplicates=bool(getattr(args, "exclude_duplicates", False)),
        page=int(getattr(args, "page", 1) or 1),
        page_size=int(getattr(args, "limit", 20) or 20),
    ).normalized()


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    """query / export 共用的筛选参数。"""
    range_help = (
        "时间快捷档："
        + "、".join(f"{key}={RANGE_LABELS[key]}" for key in RANGE_KEYS if key != "custom")
        + f"；{RANGE_LABELS['custom']} = 用 --since/--until 自己给边界。与 --since/--until 互斥"
    )
    parser.add_argument("--province", metavar="CODE", help="行政区划代码，如 440000")
    parser.add_argument("--category", choices=_CATEGORY_CHOICES, help="来源类别")
    parser.add_argument("--source", metavar="ADAPTER", help="适配器名，如 sgcc_gd")
    parser.add_argument("--range", choices=list(RANGE_KEYS), help=range_help)
    parser.add_argument("--since", metavar="DATE", help="自定义起始时间（北京时间）")
    parser.add_argument(
        "--until", metavar="DATE", help="自定义结束时间（北京时间，只给日期则含当天）"
    )
    parser.add_argument("--keyword", metavar="KW", help="标题或正文关键词")
    parser.add_argument("--body-only", action="store_true", help="只要正文非空的文章")
    parser.add_argument("--exclude-duplicates", action="store_true", help="排除转载标记的文章")


# ==========================================================================
# 初始化与自检
# ==========================================================================
def cmd_init_db(args: argparse.Namespace) -> int:
    """建库 → 建表 → 灌种子数据。"""
    db = _open_db()
    result = db.init()
    _out("数据库初始化完成：")
    _out(f"  schema 语句 : {result['statements']}")
    _out(f"  省级行政区 : {result['provinces']}")
    _out(f"  来源       : {result['sources']}")

    if args.skip_verify:
        return 0

    report = db.check()
    if not report.get("ok"):
        _out(f"  ⚠️ 复核失败: {report.get('error')}")
        return 1
    tables = report.get("tables") or []
    _out(f"  表         : {len(tables)} 张 → {', '.join(tables)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """配置 + 数据库 + 队列 + 来源/适配器 四合一自检。"""
    settings = get_settings()
    problems: list[str] = []

    _out("== 配置摘要 ==")
    for key, value in settings.describe().items():
        _out(f"  {key:<15}: {value}")

    _out("")
    _out("== MySQL ==")
    try:
        report = _open_db().check()
        if report.get("ok"):
            _out(f"  连接     : OK（{report.get('database')} / MySQL {report.get('version')}）")
            tables = report.get("tables") or []
            _out(f"  表       : {len(tables)} 张")
            expected = {
                "provinces", "sources", "source_slots", "articles",
                "urls", "article_tasks", "crawl_jobs", "crawl_runs",
                "market_prices",
            }
            missing = sorted(expected - set(tables))
            if missing:
                problems.append(f"缺表: {', '.join(missing)}（先跑 init-db）")
        else:
            problems.append(f"MySQL 不可用: {report.get('error')}")
            _out(f"  连接     : 失败 → {report.get('error')}")
    except Exception as exc:  # noqa: BLE001 - 连不上也要把配置摘要打印完
        problems.append(f"MySQL 不可用: {exc}")
        _out(f"  连接     : 失败 → {exc}")

    _out("")
    _out("== Redis ==")
    try:
        from newsagg.queue import StreamQueue

        q = StreamQueue(settings)
        if q.ping():
            stats = q.stats()
            _out(f"  连接     : OK（stream={stats.get('stream')} length={stats.get('length')}）")
        else:
            problems.append("Redis 不可用（队列功能不可用）")
            _out("  连接     : 失败")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Redis 不可用: {exc}")
        _out(f"  连接     : 失败 → {exc}")

    _out("")
    _out("== 来源 / 适配器 ==")
    try:
        from newsagg.adapters import available_adapters, ensure_loaded
        from newsagg.sources_seed import SOURCE_BY_ID, validate

        ensure_loaded()
        seed_issues = validate()
        registered = set(available_adapters())
        seed_ids = set(SOURCE_BY_ID)
        _out(f"  种子来源 : {len(seed_ids)}")
        _out(f"  已注册   : {len(registered)}")
        if seed_issues:
            problems.extend(seed_issues)
            _out(f"  种子问题 : {seed_issues}")
        missing = sorted(seed_ids - registered)
        extra = sorted(registered - seed_ids)
        if missing:
            problems.append(f"无适配器: {', '.join(missing)}")
            _out(f"  缺适配器 : {', '.join(missing)}")
        if extra:
            _out(f"  多余适配器（不在清单中）: {', '.join(extra)}")
        if not missing and not seed_issues:
            _out("  一致性   : OK")
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        problems.append(f"适配器导入失败: {exc}")
        _out(f"  一致性   : 失败 → {exc}")

    _out("")
    if problems:
        _out(f"== 结果: {len(problems)} 个问题 ==")
        for item in problems:
            _out(f"  ✗ {item}")
        return 1
    _out("== 结果: 全部通过 ==")
    return 0


def cmd_validate(_args: argparse.Namespace) -> int:
    """离线校验省字典与来源清单（不连库）。"""
    from newsagg.provinces import MAINLAND_COUNT, PROVINCES, TOTAL_COUNT
    from newsagg.sources_seed import (
        ALL_SOURCES,
        SOURCE_BY_ID,
        SOURCES,
        count_by_category,
        coverage_codes,
        validate,
    )

    _out("== 省级行政区 ==")
    _out(f"  条目总数 : {len(PROVINCES)}（大陆 {MAINLAND_COUNT} / 合计 {TOTAL_COUNT}）")

    _out("")
    _out("== 来源清单 ==")
    # 两个数都要报：基础清单（SOURCES，seed_sources 只灌这些）与含补充源的
    # 全量清单（ALL_SOURCES）。只报一个时，下面的「分类分布」会把两者对不上。
    _out(f"  基础清单 : {len(SOURCES)} 条（SOURCES）")
    _out(f"  含补充源 : {len(ALL_SOURCES)} 条（ALL_SOURCES）")
    _out(f"  分类分布 : {count_by_category()}")
    _out(f"  覆盖省级代码数 : {len(coverage_codes())}")
    _out(f"  唯一 id 数     : {len(SOURCE_BY_ID)}")

    issues = validate()
    _out("")
    if issues:
        _out(f"== 结果: {len(issues)} 个问题 ==")
        for item in issues:
            _out(f"  ✗ {item}")
        return 1
    _out("== 结果: 校验通过 ==")
    return 0


def cmd_adapters(args: argparse.Namespace) -> int:
    """列出适配器并跑契约校验。"""
    from newsagg.adapters import available_adapters, ensure_loaded, get_adapter
    from newsagg.adapters.sgcc_generic import assert_adapter_contract

    ensure_loaded()
    names = available_adapters()
    if not names:
        _out("未发现任何适配器。")
        return 1

    rows: list[dict[str, Any]] = []
    issues: dict[str, list[str]] = {}
    for name in names:
        adapter = get_adapter(name)
        found = assert_adapter_contract(adapter)
        if found:
            issues[name] = found
        if args.category and getattr(adapter, "category", "") != args.category:
            continue
        rows.append(
            {
                "adapter": name,
                "province": getattr(adapter, "province_code", "") or "",
                "category": getattr(adapter, "category", "") or "",
                "priority": getattr(adapter, "priority", "") or 0,
                "interval_h": getattr(adapter, "crawl_interval_hours", "") or 0,
                "column_url": getattr(adapter, "column_url", "")
                or getattr(adapter, "list_url", ""),
            }
        )

    if args.json:
        _print_json({"count": len(rows), "items": rows, "issues": issues})
        return 1 if issues else 0

    _out(f"已注册适配器 {len(names)} 个" + (f"（筛选后 {len(rows)}）" if args.category else ""))
    if rows:
        width = max(len(r["adapter"]) for r in rows)
        _out(f"  {'适配器':<{width}}  省份     类别          优先级  间隔 栏目页")
        for row in rows:
            _out(
                f"  {row['adapter']:<{width}}  {row['province']:<8} "
                f"{row['category']:<13} {str(row['priority']):<6} "
                f"{str(row['interval_h']):<4} {row['column_url']}"
            )

    _out("")
    if issues:
        _out(f"契约问题 {len(issues)} 个：")
        for name, found in issues.items():
            _out(f"  ✗ {name}: {'; '.join(found)}")
        return 1
    _out("契约校验：全部通过")
    return 0


# ==========================================================================
# 采集运行时
# ==========================================================================
def cmd_schedule(args: argparse.Namespace) -> int:
    """跑协调器（建批 + 投递）。"""
    from newsagg.schedule import main as schedule_main

    argv: list[str] = []
    if args.force:
        argv.append("--force")
    if args.dry_run:
        argv.append("--dry-run")
    if args.loop:
        argv.append("--loop")
    return schedule_main(argv)


def cmd_crawl_once(args: argparse.Namespace) -> int:
    """手工建批（可绕过采集间隔 / 冷却），可选直接执行。"""
    import subprocess

    from newsagg.cleaning import now_cst, now_utc_naive
    from newsagg.db import Database
    from newsagg.queue import StreamQueue
    from newsagg.repo import Repository
    from newsagg.timewindow import JOB_FULL_RESCAN, build_window

    settings = get_settings()
    db = Database(settings)
    repo = Repository(db)
    now_utc = now_utc_naive()
    stamp = now_cst().strftime("%Y%m%d%H%M%S")
    window_days = max(1, int(args.days or settings.window_days))

    conn = db.connect()
    job_ids: list[int] = []
    planned = 0
    try:
        sources = repo.list_sources(conn, enabled_only=not args.include_disabled)
        wanted = set(args.source or [])
        if args.province:
            wanted |= {
                s.adapter_name for s in sources if str(s.province_code) == str(args.province)
            }
        if wanted:
            sources = [s for s in sources if s.adapter_name in wanted]
        if args.limit:
            sources = sources[: args.limit]

        if not sources:
            _out("没有匹配的来源（检查 --source / --province / 启用状态）。")
            return 1

        for src in sources:
            window = build_window(
                job_type=JOB_FULL_RESCAN,
                now_utc_naive=now_utc,
                window_days=window_days,
            )
            if args.dry_run:
                # 手工建批的 trigger_key 带秒级时间戳，本来就不会撞唯一键，
                # 所以 dry-run 不必真插一行才能知道结果——写下去就不叫试运行了。
                _out(f"  将建批 {src.adapter_name:<16} {window.describe()}")
                planned += 1
                continue
            job_id = repo.create_job(
                conn,
                source_id=src.id,
                trigger_key=f"manual:{stamp}:{src.adapter_name}",
                job_type=window.job_type,
                window_start_cst=window.start_cst,
                window_end_cst=window.end_cst,
                priority=src.priority,
            )
            if job_id:
                job_ids.append(job_id)
                _out(f"  建批 {src.adapter_name:<16} job={job_id} {window.describe()}")
        conn.commit()
    finally:
        conn.close()

    if args.dry_run:
        _out(f"[dry-run] 共 {planned} 个来源可建批，未写库、不投递、不执行。")
        return 0

    if not job_ids:
        _out("未新建任务（trigger_key 冲突或无可用来源）。")
        return 0

    if args.exec:
        _out(f"开始串行执行 {len(job_ids)} 个任务（每任务独占进程）…")
        python = sys.executable or "python"
        failed = 0
        for job_id in job_ids:
            cmd = [python, "-m", "newsagg", "run-job", str(job_id)]
            if args.refresh:
                cmd.append("--refresh")
            code = subprocess.run(cmd, check=False).returncode
            if code != 0:
                failed += 1
                _out(f"  ✗ job {job_id} 退出码 {code}")
        _out(f"执行完成：成功 {len(job_ids) - failed} / 失败 {failed}")
        return 1 if failed else 0

    published = StreamQueue(settings).publish(job_ids)
    _out(f"已投递 {published} 条任务到队列（stream={settings.queue_stream}）。")
    return 0


def cmd_run_job(args: argparse.Namespace) -> int:
    """同步跑单个任务（worker 内部就是拉起这个）。"""
    from newsagg.crawl.engine import run_blocking

    owner = args.owner or f"cli-{args.job_id}"
    bind_context(job_id=args.job_id)
    try:
        stats = run_blocking(args.job_id, owner=owner, refresh=args.refresh)
    except PowerNewsError as exc:
        _out(f"任务未执行: {exc}")
        return 1

    if args.json:
        _print_json(stats)
        return 0 if stats.get("status") in ("succeeded", "partial") else 1

    _out("任务完成：")
    for key, value in stats.items():
        _out(f"  {key:<12}: {value}")
    return 0 if stats.get("status") in ("succeeded", "partial") else 1


def cmd_worker(args: argparse.Namespace) -> int:
    """单个 worker 主循环。"""
    from newsagg.worker import run_worker, worker_identity

    owner = args.owner or worker_identity()
    run_worker(owner=owner, once=args.once)
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    """多进程监督 N 个 worker。"""
    from newsagg.supervisor import run_supervisor

    run_supervisor(args.workers)
    return 0


# ==========================================================================
# 数据消费
# ==========================================================================
def cmd_stats(args: argparse.Namespace) -> int:
    """采集统计（概览 / 按省份 / 按来源 / 字段质量 / 任务健康）。"""
    from newsagg import stats as stats_mod

    db = _open_db()
    scope = args.scope

    if scope == "province":
        data = stats_mod.by_province(db, limit=args.top)
        title = f"按省份（前 {args.top}）"
    elif scope == "source":
        data = stats_mod.by_source(db, limit=args.top)
        title = f"按来源（前 {args.top}）"
    elif scope == "quality":
        data = stats_mod.quality(db)
        title = "字段质量"
    elif scope == "jobs":
        data = stats_mod.job_health(db)
        title = "任务健康度"
    else:
        data = stats_mod.overview(db)
        title = "全局概览"

    if args.json:
        _print_json(data)
        return 0
    _out(stats_mod.render_text(data, title))
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    """来源清单（优先读库，库不可用时回退种子定义）。"""
    from newsagg.sources_seed import SOURCE_BY_ID, list_url

    rows: list[dict[str, Any]] = []
    from_db = False
    try:
        db = _open_db()
        conn = db.connect()
        try:
            from newsagg.repo import Repository

            for src in Repository(db).list_sources(
                conn,
                category=args.category,
                province_code=args.province,
                enabled_only=args.enabled,
            ):
                rows.append(
                    {
                        "adapter": src.adapter_name,
                        "name": src.name,
                        "province": src.province_code or "",
                        "category": src.category or "",
                        "priority": src.priority or 0,
                        "interval_h": src.crawl_interval_hours or 0,
                        "enabled": int(src.enabled or 0),
                        "list_url": src.list_url,
                    }
                )
            from_db = True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 库不可用时静默回退到种子定义
        log.warning("读库失败，回退种子定义: %s", exc)

    if not from_db:
        for src in SOURCE_BY_ID.values():
            if args.category and src["category"] != args.category:
                continue
            if args.province and str(src["province_code"]) != str(args.province):
                continue
            if args.enabled and not src["enabled"]:
                continue
            rows.append(
                {
                    "adapter": src["id"],
                    "name": src["name"],
                    "province": src["province_code"] or "",
                    "category": src["category"] or "",
                    "priority": src["priority"] or 0,
                    "interval_h": src["interval_hours"] or 0,
                    "enabled": 1 if src["enabled"] else 0,
                    "list_url": list_url(src),
                }
            )

    if args.json:
        _print_json({"origin": "db" if from_db else "seed", "count": len(rows), "items": rows})
        return 0

    _out(f"来源 {len(rows)} 条（数据源: {'数据库' if from_db else '种子定义'}）")
    if not rows:
        return 0
    w_adapter = max(12, max(len(r["adapter"]) for r in rows))
    w_name = min(28, max(8, max(len(r["name"]) for r in rows)))
    _out(f"  {'适配器':<{w_adapter}}  {'省份':<8}{'类别':<13}{'优先级':<7}{'间隔':<5}{'启用':<5} 名称")
    for row in rows:
        _out(
            f"  {row['adapter']:<{w_adapter}}  {str(row['province']):<8}{row['category']:<13}"
            f"{str(row['priority']):<7}{str(row['interval_h']):<5}"
            f"{'是' if row['enabled'] else '否':<5} {row['name'][:w_name]}"
        )
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """分页检索文章。"""
    from newsagg.query import query_articles

    f = _filters_from_args(args)
    result = query_articles(f, _open_db())

    if args.json:
        _print_json(result)
        return 0

    if getattr(args, "range", None):
        _out(f"时间范围：{describe_range(args.range)}")
    _out(
        f"共 {result['total']} 条，第 {result['page']}/{max(1, result['pages'])} 页"
        f"（每页 {result['page_size']}）"
    )
    if not result["items"]:
        return 0
    for item in result["items"]:
        when = item.get("published_at_cst") or "未知时间"
        _out(
            f"  #{item['id']:<7} {str(when):<20} "
            f"[{item.get('province_name') or item.get('province_code') or '-'}] "
            f"{(item.get('title') or '(无标题)')[:60]}"
        )
        if args.show_url:
            _out(f"          {item.get('url')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """查看单篇文章。"""
    from newsagg.query import get_article

    article = get_article(args.article_id, _open_db())
    if not article:
        _out(f"未找到文章 #{args.article_id}")
        return 1

    if args.json:
        _print_json(article)
        return 0

    _out(f"编号     : {article['id']}")
    _out(f"标题     : {article.get('title') or '(无标题)'}")
    _out(f"省份     : {article.get('province_name') or '-'}（{article.get('province_code') or '-'}）")
    _out(f"来源     : {article.get('source_name') or article.get('source_name_full') or '-'}")
    _out(f"发布时间 : {article.get('published_at_cst') or '未知'}（{article.get('time_precision')}）")
    _out(f"质量标记 : {article.get('quality_flags') or '-'}")
    _out(f"原文链接 : {article.get('url')}")
    if not args.no_body:
        _out("")
        _out("---- 正文 ----")
        _out(article.get("body_text") or "(正文为空)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """导出 CSV。"""
    from newsagg.export import export_csv
    from newsagg.query import count_articles

    f = _filters_from_args(args)
    db = _open_db()
    total = count_articles(f, db)
    if total == 0:
        _out("没有匹配的文章，未生成文件。")
        return 1

    out_path = args.out
    if not out_path:
        out_path = f"exports/powernews-{datetime.now():%Y%m%d-%H%M%S}.csv"
    result = export_csv(f, out_path, db)
    _out(
        f"已导出 {result['rows']} 行 → {result['path']}"
        f"（{result['bytes'] / 1024:.1f} KB）"
    )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Redis 队列概览。"""
    from newsagg.queue import StreamQueue

    q = StreamQueue(get_settings())
    if not q.ping():
        _out("Redis 不可用，无法读取队列。")
        return 1

    if args.reset_group:
        q.reset_group()
        _out(f"消费组 {q.group} 已销毁。")

    info = q.stats()
    if args.json:
        _print_json(info)
        return 0

    _out(f"流名   : {info.get('stream')}")
    _out(f"长度   : {info.get('length')}")
    groups = info.get("groups") or []
    if not groups:
        _out("消费组 : (无)")
        return 0
    _out("消费组 :")
    for g in groups:
        _out(
            f"  {g.get('name')}: consumers={g.get('consumers')} "
            f"pending={g.get('pending')} last_delivered={g.get('last_delivered_id')}"
        )
    return 0


# ==========================================================================
# 市场行情
# ==========================================================================
def cmd_market_fetch(args: argparse.Namespace) -> int:
    """抓取市场行情并幂等入库。"""
    from newsagg.market.cli import market_fetch

    argv = ["--dataset", args.dataset, "--periods", str(args.periods)]
    if args.dry_run:
        argv.append("--dry-run")
    if args.json:
        argv.append("--json")
    return market_fetch(argv)


def cmd_fetch_images(args: argparse.Namespace) -> int:
    """把 `article_images` 里 pending 的配图下载并上传 RustFS。"""
    from newsagg.config import get_settings
    from newsagg.db import Database
    from newsagg.imagestore import ImageStore
    from newsagg.repo import Repository

    settings = get_settings()
    db = Database(settings)
    store = ImageStore(settings, Repository(db), db)

    total = {"processed": 0, "downloaded": 0, "failed": 0, "skipped": 0}
    rounds = 0
    while True:
        rounds += 1
        stats = store.process_pending(limit=args.batch)
        if not stats["processed"]:
            break
        for k in total:
            total[k] += stats.get(k, 0)
        print(f"  第 {rounds} 批: 处理 {stats['processed']} "
              f"(成功 {stats.get('downloaded', 0)} / 失败 {stats.get('failed', 0)} "
              f"/ 跳过 {stats.get('skipped', 0)})", flush=True)
        if args.max_rounds and rounds >= args.max_rounds:
            break

    print("=" * 56)
    print(f"合计处理 {total['processed']} 张")
    print(f"  ✓ 已上传 RustFS : {total['downloaded']}")
    print(f"  ✗ 失败         : {total['failed']}")
    print(f"  - 跳过(无效图)  : {total['skipped']}")
    print(f"桶: {store.bucket}")
    print("=" * 56)
    return 0


def cmd_market_stats(args: argparse.Namespace) -> int:
    """查看 ``market_prices`` 覆盖情况。"""
    from newsagg.market.cli import market_stats

    argv = ["--dataset", args.dataset]
    if args.latest:
        argv += ["--latest", str(args.latest)]
    if args.json:
        argv.append("--json")
    return market_stats(argv)


# ==========================================================================
# 气象数据（Open-Meteo）
# ==========================================================================
#: ``powernews weather <子命令>`` 的子命令表。参数**原样透传**给
#: ``newsagg.weather.cli`` —— 气象侧参数较多（产品/窗口/大区/批量/限速），
#: 在主 CLI 里再镜像一遍会形成第二份定义，改一处漏一处。
WEATHER_COMMANDS = ("init", "backfill", "daily", "stats", "check")


def cmd_weather(args: argparse.Namespace) -> int:
    """气象数据子命令入口。"""
    from newsagg.weather import cli as weather_cli

    table = {
        "init": weather_cli.weather_init,
        "backfill": weather_cli.weather_backfill,
        "daily": weather_cli.weather_daily,
        "stats": weather_cli.weather_stats,
        "check": weather_cli.weather_check,
    }
    return table[args.weather_command](args.rest)


# ==========================================================================
# 服务
# ==========================================================================
def cmd_serve(args: argparse.Namespace) -> int:
    """起数据中台（newsplatform）。"""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        _out(
            "未安装数据中台依赖。请执行：\n"
            "  pip install -r newsplatform/requirements.txt\n"
            "或：pip install -e .[platform]"
        )
        return 1

    try:
        from newsplatform.main import create_app
    except ImportError as exc:
        _out(f"无法加载 newsplatform.main: {exc}")
        return 1

    import uvicorn

    app = create_app()
    _out(f"数据中台启动中 → http://{args.host}:{args.port}{args.docs_path}")
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


# ==========================================================================
# 参数解析
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    """构造完整命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="powernews",
        description="Power News Aggregator —— 34 个省级行政区电力新闻采集与数据中台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  powernews init-db\n"
            "  powernews check\n"
            "  powernews adapters\n"
            "  powernews crawl-once --province 440000 --days 3\n"
            "  powernews supervisor --workers 8\n"
            "  powernews query --province 440000 --keyword 特高压 --limit 10\n"
            "  powernews export --since 2026-10-01 --out exports/oct.csv\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"powernews {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 级日志")
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    # ---- init-db ----
    p = sub.add_parser("init-db", help="建库 → 建表 → 灌 34 省 + 39 来源")
    p.add_argument("--skip-verify", action="store_true", help="灌完不做复核")
    p.set_defaults(func=cmd_init_db)

    # ---- check ----
    p = sub.add_parser("check", help="配置 / MySQL / Redis / 来源适配器 四合一自检")
    p.set_defaults(func=cmd_check)

    # ---- validate ----
    p = sub.add_parser("validate", help="离线校验省字典与来源清单")
    p.set_defaults(func=cmd_validate)

    # ---- adapters ----
    p = sub.add_parser("adapters", help="列出适配器并跑契约校验")
    p.add_argument("--category", choices=_CATEGORY_CHOICES, help="只看某类别")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_adapters)

    # ---- schedule ----
    p = sub.add_parser("schedule", help="跑一轮协调（建批 + 投递）")
    p.add_argument("--force", action="store_true", help="忽略采集间隔，全部来源建批")
    p.add_argument("--dry-run", action="store_true", help="只建批不投递")
    p.add_argument("--loop", action="store_true", help="常驻，每 10 分钟一轮")
    p.set_defaults(func=cmd_schedule)

    # ---- crawl-once ----
    p = sub.add_parser("crawl-once", help="手工建批（可绕过间隔/冷却），可选直接执行")
    p.add_argument("--source", action="append", metavar="ADAPTER",
                   help="只跑指定适配器（可重复）")
    p.add_argument("--province", metavar="CODE", help="只跑指定省份的来源")
    p.add_argument("--days", type=int, default=None, help="回看天数（默认取配置 window_days）")
    p.add_argument("--limit", type=int, default=0, help="最多建批几个来源（0 = 不限）")
    p.add_argument("--include-disabled", action="store_true", help="含已停用来源")
    p.add_argument("--refresh", action="store_true", help="配合 --exec 重抓已发现候选")
    p.add_argument("--exec", action="store_true", help="建批后串行直接执行（不等 worker）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要建批的来源")
    p.set_defaults(func=cmd_crawl_once)

    # ---- run-job ----
    p = sub.add_parser("run-job", help="同步跑单个任务（worker 内部调用）")
    p.add_argument("job_id", type=int, help="任务 id")
    p.add_argument("--refresh", action="store_true", help="重抓已发现候选以补元数据")
    p.add_argument("--owner", metavar="NAME", help="租约持有者标识")
    p.add_argument("--json", action="store_true", help="以 JSON 输出统计")
    p.set_defaults(func=cmd_run_job)

    # ---- worker ----
    p = sub.add_parser("worker", help="单个 worker 主循环")
    p.add_argument("--once", action="store_true", help="只处理一轮后退出")
    p.add_argument("--owner", metavar="NAME", help="worker 身份（默认主机名-PID）")
    p.set_defaults(func=cmd_worker)

    # ---- supervisor ----
    p = sub.add_parser("supervisor", help="多进程监督 N 个 worker")
    p.add_argument("--workers", type=int, default=None, help="worker 数量（默认取配置）")
    p.set_defaults(func=cmd_supervisor)

    # ---- stats ----
    p = sub.add_parser("stats", help="采集统计")
    p.add_argument("--scope", choices=_STATS_CHOICES, default="overview",
                   help="统计维度（默认 overview）")
    p.add_argument("--top", type=int, default=40, help="按省份/来源时的返回条数")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_stats)

    # ---- sources ----
    p = sub.add_parser("sources", help="来源清单")
    p.add_argument("--category", choices=_CATEGORY_CHOICES, help="按类别筛选")
    p.add_argument("--province", metavar="CODE", help="按省份筛选")
    p.add_argument("--enabled", action="store_true", help="只看已启用")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_sources)

    # ---- query ----
    p = sub.add_parser("query", help="分页检索文章")
    _add_filter_args(p)
    p.add_argument("-n", "--limit", type=int, default=20, help="每页条数（上限 100）")
    p.add_argument("--page", type=int, default=1, help="页码")
    p.add_argument("--show-url", action="store_true", help="同时打印原文链接")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_query)

    # ---- show ----
    p = sub.add_parser("show", help="查看单篇文章全文")
    p.add_argument("article_id", type=int, help="文章 id")
    p.add_argument("--no-body", action="store_true", help="不打印正文")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_show)

    # ---- export ----
    p = sub.add_parser("export", help="导出 CSV（UTF-8 BOM）")
    _add_filter_args(p)
    p.add_argument("--out", metavar="PATH", help="输出路径（默认 exports/ 下按时间命名）")
    p.set_defaults(func=cmd_export)

    # ---- queue ----
    p = sub.add_parser("queue", help="Redis 队列概览")
    p.add_argument("--reset-group", action="store_true", help="先销毁消费组（运维用）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_queue)

    # ---- market-fetch ----
    from newsagg.market.models import DATASETS as _MARKET_DATASETS

    _market_choices = ["all", *sorted(_MARKET_DATASETS)]
    p = sub.add_parser("market-fetch", help="抓取市场行情（LNG 现价 / CECI 电煤指数 / 电力现货）")
    p.add_argument("--dataset", default="all", choices=_market_choices, help="要抓的数据集")
    p.add_argument("--periods", type=int, default=1,
                   help="抓最近多少期（LNG 按天、CECI/现货按周）")
    p.add_argument("--dry-run", action="store_true", help="只抓不写库（不需要 MySQL）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_market_fetch)

    # ---- market-stats ----
    p = sub.add_parser("market-stats", help="查看 market_prices 覆盖情况与最新数据")
    p.add_argument("--dataset", default="all", choices=_market_choices, help="只看某个数据集")
    p.add_argument("--latest", type=int, default=0, metavar="N",
                   help="改为列出最新 N 条明细（默认 0 = 覆盖汇总）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_market_stats)

    # ---- weather ----
    p = sub.add_parser(
        "weather",
        help="气象数据（Open-Meteo）：建表 / 回填 / 每日采集 / 统计 / 自检",
    )
    p.add_argument(
        "weather_command", choices=list(WEATHER_COMMANDS), metavar="<子命令>",
        help="init 建表｜backfill 长回填｜daily 每日采集｜stats 统计｜check 自检",
    )
    p.add_argument(
        "rest", nargs=argparse.REMAINDER,
        help="透传给子命令的参数，例如 `--since 2024-01-01 --regions 华东,华南`",
    )
    p.set_defaults(func=cmd_weather)

    # ---- serve ----
    # ---- fetch-images ----
    p = sub.add_parser("fetch-images",
                       help="下载正文配图并上传 RustFS（消费 article_images 的 pending 队列）")
    p.add_argument("--batch", type=int, default=200, help="单批处理张数")
    p.add_argument("--max-rounds", type=int, default=0, help="最多跑几批（0=直到没有 pending）")
    p.set_defaults(func=cmd_fetch_images)

    p = sub.add_parser("serve", help="起数据中台（newsplatform）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址")
    p.add_argument("--port", type=int, default=8000, help="监听端口")
    p.add_argument("--reload", action="store_true", help="开发模式自动重载")
    p.add_argument("--docs-path", default="/docs", help="OpenAPI 文档路径（仅用于提示）")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    _ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    setup_logging(level="DEBUG" if args.verbose else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except PowerNewsError as exc:
        _out(f"错误: {exc}")
        return 2
    except KeyboardInterrupt:
        _out("已中断。")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
