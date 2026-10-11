"""气象采集命令行。

命令一览::

    weather-init      建 24 张表 + 同步站点主表（幂等）
    weather-backfill  长回填，按「产品 × 时间窗 × 大区」切片，可断点续跑
    weather-daily     每日滚动任务（每日预测 + 历史预测 + 历史实时）
    weather-stats     查看 24 张表的行数与时间覆盖
    weather-check     自检：表是否齐全、站点是否同步、端点是否可达

回填为什么要按「大区」而不是「全部站点」切片
--------------------------------------------
回填 2024-01-01 至今是百万行量级、按小时计的任务，中途必然被打断。
进度表的主键是 ``(产品, 大区, 时间窗)``——只有把大区当成工作单元，
「华东已回填到 6 月、华南还停在 3 月」才能被精确记录并续跑。
若把 8 个大区揉成一批，任何一次中断都会让整批作废重来。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from newsagg.db import Database
from newsagg.logutil import get_logger
from newsagg.weather.client import RateLimiter, Station, WeatherClient
from newsagg.weather.models import (
    DAILY_FROM_API,
    ENDPOINTS,
    HOURLY_VARS,
    PRODUCTS,
    REGIONS,
    chunk_periods,
    estimate_call_cost,
)
from newsagg.weather.schedule import DEFAULT_LOOKBACK_DAYS, run_daily
from newsagg.weather.store import WeatherStore

__all__ = [
    "weather_init", "weather_backfill", "weather_daily",
    "weather_stats", "weather_check", "main",
]

log = get_logger("weather.cli")

DEFAULT_SINCE = "2024-01-01"


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"日期格式应为 YYYY-MM-DD，收到 {text!r}") from exc


def _build_context(args: argparse.Namespace) -> tuple[WeatherStore, WeatherClient]:
    store = WeatherStore(Database())
    limiter = RateLimiter(
        per_minute=getattr(args, "per_minute", 600),
        per_hour=getattr(args, "per_hour", 5_000),
        per_day=getattr(args, "per_day", 10_000),
    )
    client = WeatherClient(
        batch=args.batch,
        period_days=args.period_days,
        limiter=limiter,
    )
    return store, client


# --------------------------------------------------------------------------- init


def weather_init(argv: Sequence[str] | None = None) -> int:
    """建表 + 同步站点主表。"""
    ap = argparse.ArgumentParser(prog="weather-init", description="建气象数据表并同步站点")
    ap.add_argument("--no-stations", action="store_true", help="只建表，不同步站点")
    args = ap.parse_args(list(argv) if argv is not None else None)

    store = WeatherStore(Database())
    count = store.ensure_schema()
    print(f"已执行建表语句 {count} 条")
    missing = store.missing_tables()
    if missing:
        print(f"⚠ 仍缺 {len(missing)} 张表：{missing[:5]}")
        return 1
    print("24 张数据表齐全")

    if not args.no_stations:
        rows = store.sync_stations()
        print(f"站点主表已同步 {rows} 行")
    return 0


# --------------------------------------------------------------------------- backfill


def _plan_units(
    *,
    products: Sequence[str],
    regions: Sequence[str],
    start: date,
    end: date,
    period_days: int,
) -> list[tuple[str, str, date, date]]:
    """展开成 ``(产品, 大区, 窗口起, 窗口止)`` 工作单元列表。

    滚动型产品（``daily_fc``）**不按时间切片**：它的窗口会被
    :func:`resolve_window` 恒夹到「今天起」，若照常切成 73 段，
    每一段解析后都指向同一天 → 同一天被请求 73 次并互相覆盖，
    既浪费额度也让进度表出现 73 条自相矛盾的完成记录。
    """
    units: list[tuple[str, str, date, date]] = []
    for product in products:
        style = ENDPOINTS[product][1]
        for region in regions:
            if style == "rolling":
                units.append((product, region, start, end))
            else:
                for p_start, p_end in chunk_periods(start, end, days=period_days):
                    units.append((product, region, p_start, p_end))
    return units


def weather_backfill(argv: Sequence[str] | None = None) -> int:
    """长回填（可续跑）。"""
    ap = argparse.ArgumentParser(prog="weather-backfill", description="气象数据回填")
    ap.add_argument("--product", default="all",
                    choices=["all", *PRODUCTS.keys()], help="数据产品")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="起始日（闭）")
    ap.add_argument("--until", default=None, help="结束日（闭），默认今天")
    ap.add_argument("--regions", default="all",
                    help="大区，逗号分隔；all 表示全部八个")
    ap.add_argument("--batch", type=int, default=100, help="单请求站点数")
    ap.add_argument("--period-days", type=int, default=14, help="单请求时间跨度（天）")
    ap.add_argument("--per-minute", type=int, default=600, help="本地限速：次/分")
    ap.add_argument("--per-hour", type=int, default=5_000, help="本地限速：次/时")
    ap.add_argument("--per-day", type=int, default=10_000, help="本地限速：次/日")
    ap.add_argument("--no-resume", action="store_true", help="忽略进度表，全部重跑")
    ap.add_argument("--dry-run", action="store_true", help="只估算请求量，不取数不落库")
    args = ap.parse_args(list(argv) if argv is not None else None)

    products = list(PRODUCTS) if args.product == "all" else [args.product]
    regions = (
        list(REGIONS) if args.regions == "all"
        else [r.strip() for r in args.regions.split(",") if r.strip()]
    )
    unknown = set(regions) - set(REGIONS)
    if unknown:
        raise SystemExit(f"未知大区 {sorted(unknown)}，可选 {list(REGIONS)}")

    start = _parse_date(args.since)
    end = _parse_date(args.until) if args.until else date.today()

    store, client = _build_context(args)
    try:
        units = _plan_units(
            products=products, regions=regions,
            start=start, end=end, period_days=args.period_days,
        )
        if args.dry_run:
            _print_dry_run(units, store, client)
            return 0

        stations_by_region = {
            r: store.load_stations(regions=[r]) for r in regions
        }
        done_total = 0
        skipped = 0
        try:
            for product, region, p_start, p_end in units:
                if not args.no_resume:
                    done = store.completed_periods(product=product, region=region)
                    if (p_start, p_end) in done:
                        skipped += 1
                        continue
                stations: list[Station] = stations_by_region[region]
                if not stations:
                    continue
                log.info("→ %s / %s / %s ~ %s（%d 站）",
                         product, region, p_start, p_end, len(stations))
                stats = None
                try:
                    from newsagg.weather.schedule import run_product

                    stats, written = run_product(
                        product=product, stations=stations,
                        start=p_start, end=p_end,
                        store=store, client=client,
                    )
                    done_total += written
                    log.info("  %s｜累计写入 %d 行", stats.describe(), written)
                except Exception as exc:  # noqa: BLE001 - 单片失败不该终止整轮回填
                    log.error("  该片失败：%s", exc)
                    store.mark_progress(
                        product=product, region=region,
                        period_start=p_start, period_end=p_end,
                        hourly_rows=0, daily_rows=0, stations=len(stations),
                        status="failed", note=str(exc),
                    )
                    continue
        except KeyboardInterrupt:
            print("\n已中断——进度已按大区记录，重新执行本命令即可续跑。")
            return 130
    finally:
        client.close()

    print(f"\n回填完成：写入 {done_total} 行，跳过已完成 {skipped} 片")
    return 0


def _print_dry_run(
    units: list[tuple[str, str, date, date]],
    store: WeatherStore,
    client: WeatherClient,
) -> None:
    """不取数，只估算请求量与折算调用次数。"""
    print("模式：dry-run（不取数、不落库）\n")
    total_requests = 0
    total_cost = 0.0
    #: 单请求实际携带的变量数 = 逐时变量 + 日聚合参数（后者会让计费倍数上升）
    variables = len(HOURLY_VARS) + len(DAILY_FROM_API)
    per_product: dict[str, list[int | float]] = {}
    today = date.today()
    for product, region, p_start, p_end in units:
        n = len(store.load_stations(regions=[region]))
        batches = max(1, -(-n // client.batch))
        if ENDPOINTS[product][1] == "rolling":
            # 滚动接口只会返回「今天 + 至多 16 天」，按请求的全区间算成本
            # 会虚报两个数量级（实测 1015 天 → 4067 次，实际 7 天 → 1.7 次）
            days = min(16, max(1, (p_end - max(p_start, today)).days + 1))
        else:
            days = (p_end - p_start).days + 1
        cost = batches * estimate_call_cost(
            locations=min(n, client.batch), days=days, variables=variables,
        )
        total_requests += batches
        total_cost += cost
        slot = per_product.setdefault(product, [0, 0.0])
        slot[0] = int(slot[0]) + batches
        slot[1] = float(slot[1]) + cost
    for product, (req, cost) in per_product.items():
        print(f"  {product:9s} {PRODUCTS[product]:22s} 请求约 {int(req):6d} 次 / 折算调用 {cost:9.1f} 次")
    print(f"\n合计：请求约 {total_requests} 次，折算调用约 {total_cost:.1f} 次")
    print("免费档额度：600/分 · 5,000/时 · 10,000/日 · 300,000/月")


# --------------------------------------------------------------------------- daily


def weather_daily(argv: Sequence[str] | None = None) -> int:
    """每日滚动任务。"""
    ap = argparse.ArgumentParser(prog="weather-daily", description="气象每日采集")
    ap.add_argument("--regions", default="all", help="大区，逗号分隔；all 表示全部")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="历史回看天数")
    ap.add_argument("--forecast-days", type=int, default=7, help="预报天数（≤16）")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--period-days", type=int, default=14)
    ap.add_argument("--per-minute", type=int, default=600)
    ap.add_argument("--per-hour", type=int, default=5_000)
    ap.add_argument("--per-day", type=int, default=10_000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="常驻，每天定点跑一次")
    ap.add_argument("--at", default="06:20", help="常驻模式下的执行时刻 HH:MM")
    args = ap.parse_args(list(argv) if argv is not None else None)

    regions = (
        None if args.regions == "all"
        else [r.strip() for r in args.regions.split(",") if r.strip()]
    )
    store, client = _build_context(args)
    try:
        if not args.loop:
            result = run_daily(
                store=store, client=client, regions=regions,
                lookback_days=args.lookback_days,
                forecast_days=args.forecast_days,
                dry_run=args.dry_run,
            )
            for product, rows in result.items():
                print(f"  {product:9s} {PRODUCTS[product]:22s} 写入 {rows} 行")
            return 0
        _run_loop(store, client, regions, args)
    finally:
        client.close()
    return 0


def _run_loop(store, client, regions, args) -> None:
    """常驻：每天到点跑一次。"""
    import time

    hour, minute = (int(x) for x in args.at.split(":"))
    log.info("气象每日任务常驻启动：每天 %02d:%02d 执行", hour, minute)
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info("下一次执行：%s（%.0f 分钟后）", target, wait / 60)
        try:
            time.sleep(max(1.0, wait))
        except KeyboardInterrupt:
            log.info("常驻模式停止")
            return
        try:
            run_daily(
                store=store, client=client, regions=regions,
                lookback_days=args.lookback_days,
                forecast_days=args.forecast_days,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - 单日失败不该终止常驻
            log.error("本日任务失败：%s", exc)


# --------------------------------------------------------------------------- stats


def weather_stats(argv: Sequence[str] | None = None) -> int:
    """查看 24 张表的行数与覆盖。"""
    ap = argparse.ArgumentParser(prog="weather-stats", description="气象数据覆盖统计")
    ap.add_argument("--region", default="all", help="只看某个大区")
    ap.add_argument("--product", default="all", choices=["all", *PRODUCTS.keys()])
    args = ap.parse_args(list(argv) if argv is not None else None)

    store = WeatherStore(Database())
    rows = store.summary()
    if args.region != "all":
        rows = [r for r in rows if r["region"] == args.region]
    if args.product != "all":
        rows = [r for r in rows if r["product"] == args.product]

    print(f"{'表名':<26}{'大区':<8}{'产品':<10}{'行数':>10}{'站点':>7}  {'起':<17}{'止':<17}")
    print("-" * 100)
    total = 0
    for r in rows:
        total += r["rows"]
        ts_from = str(r["ts_from"] or "—")[:16]
        ts_to = str(r["ts_to"] or "—")[:16]
        print(f"{r['table']:<26}{r['region']:<8}{r['product']:<10}"
              f"{r['rows']:>10}{r['stations']:>7}  {ts_from:<17}{ts_to:<17}")
    print("-" * 100)
    print(f"合计 {total} 行（{len(rows)} 张表）")
    return 0


# --------------------------------------------------------------------------- check


def weather_check(argv: Sequence[str] | None = None) -> int:
    """自检：表是否齐全、站点是否同步、三个端点是否可达。"""
    ap = argparse.ArgumentParser(prog="weather-check", description="气象模块自检")
    ap.add_argument("--offline", action="store_true", help="跳过端点连通性检查")
    args = ap.parse_args(list(argv) if argv is not None else None)

    ok = True
    store = WeatherStore(Database())
    missing = store.missing_tables()
    print(f"[表] 24 张数据表：{'齐全' if not missing else f'缺 {len(missing)} 张 {missing[:3]}'}")
    ok = ok and not missing

    try:
        stations = store.load_stations()
        by_region: dict[str, int] = {}
        for s in stations:
            from newsagg.weather.models import region_of_province

            r = region_of_province(s.province_code)
            by_region[r] = by_region.get(r, 0) + 1
        print(f"[站点] {len(stations)} 个；按大区 " +
              "、".join(f"{r}:{by_region.get(r, 0)}" for r in REGIONS))
        if len(stations) < 2000:
            print("  ⚠ 站点数少于 2000，是否漏跑 build_weather_stations.py？")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"[站点] 读取失败：{exc}")
        ok = False

    if args.offline:
        print("[端点] 已跳过（--offline）")
    else:
        import httpx

        from newsagg.weather.client import ENDPOINTS

        for product, (url, _) in ENDPOINTS.items():
            try:
                resp = httpx.get(
                    url,
                    params={"latitude": 39.9042, "longitude": 116.4074,
                            "hourly": "temperature_2m", "forecast_days": 1},
                    timeout=30.0,
                )
                flag = "可达" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                flag = f"不可达（{type(exc).__name__}）"
            print(f"[端点] {product:9s} {flag}")
            ok = ok and flag == "可达"
    print("\n自检结果：" + ("通过" if ok else "存在未通过项"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    """独立入口：``python -m newsagg.weather <子命令>``。"""
    ap = argparse.ArgumentParser(prog="python -m newsagg.weather", description="气象数据采集")
    sub = ap.add_subparsers(dest="command", metavar="<子命令>")
    commands = {
        "init": (weather_init, "建 24 张表 + 同步站点"),
        "backfill": (weather_backfill, "长回填（可续跑）"),
        "daily": (weather_daily, "每日滚动采集"),
        "stats": (weather_stats, "覆盖统计"),
        "check": (weather_check, "自检"),
    }
    for name, (_, help_text) in commands.items():
        sub.add_parser(name, help=help_text)
    args, rest = ap.parse_known_args(list(argv) if argv is not None else None)
    if args.command is None:
        ap.print_help()
        return 1
    return commands[args.command][0](rest)


if __name__ == "__main__":
    sys.exit(main())
