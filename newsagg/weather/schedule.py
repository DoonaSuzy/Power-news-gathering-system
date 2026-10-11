"""每日滚动采集的编排。

三类数据各自的更新节奏不同，这个模块把它们拼成一个**每日一次**的任务：

====================  ==========================================  ============================
产品                  每次覆盖的窗口                              为什么是这个窗口
====================  ==========================================  ============================
``daily_fc`` 每日预测   ``[今天, 今天+6]``                          预报接口默认给 7 天；多取几天可让下游做「预报漂移」对比
``hist_fc`` 历史预测    ``[今天−回看天数, 今天]``                    归档预报**无发布滞后**，用来补最近几天的空档
``hist_rt`` 历史实时    ``[今天−回看天数−5, 今天−5]``                ERA5 再分析滞后 5 天，最近的 5 天它还没有
====================  ==========================================  ============================

「历史实时只到 5 天前」不是缺陷而是上游发布节奏：ERA5 是再分析产品，
必须先同化观测才能出结果。**最近 5 天的实况缺口由历史预测填**——
这正是同时保留两个产品的意义，两个窗口在时间轴上首尾相接、不留空档。

每日任务因此天然是**幂等**的：昨天的预报今天重取会被主键覆盖，
而预报被上游修订（模式刷新后订正）时，覆盖正是我们想要的行为。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from newsagg.logutil import get_logger
from newsagg.weather.client import FetchStats, Station, WeatherClient
from newsagg.weather.models import ARCHIVE_LAG_DAYS, PRODUCTS
from newsagg.weather.store import WeatherStore

__all__ = ["DailyPlan", "build_daily_plan", "run_daily", "run_product"]

log = get_logger("weather.schedule")

#: 默认回看天数。7 天足以覆盖 ERA5 的 5 天滞后 + 2 天缓冲，
#: 也覆盖了「某天任务没跑、第二天补上」的常见漏跑场景。
DEFAULT_LOOKBACK_DAYS = 7


@dataclass(frozen=True, slots=True)
class DailyPlan:
    """一条待执行的工作项。"""

    product: str
    start: date
    end: date

    def describe(self) -> str:
        return f"{self.product}（{PRODUCTS[self.product]}） {self.start} ~ {self.end}"


def build_daily_plan(
    *,
    today: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forecast_days: int = 7,
) -> list[DailyPlan]:
    """构造每日任务的三个工作项；窗口为空的项会被剔除。"""
    today = today or date.today()
    lookback_days = max(1, lookback_days)
    forecast_days = max(1, min(16, forecast_days))

    plans = [
        DailyPlan("daily_fc", today, today + timedelta(days=forecast_days - 1)),
        DailyPlan("hist_fc", today - timedelta(days=lookback_days), today),
        DailyPlan(
            "hist_rt",
            today - timedelta(days=lookback_days + ARCHIVE_LAG_DAYS),
            today - timedelta(days=ARCHIVE_LAG_DAYS),
        ),
    ]
    return [p for p in plans if p.start <= p.end]


def run_product(
    *,
    product: str,
    stations: Sequence[Station],
    start: date,
    end: date,
    store: WeatherStore,
    client: WeatherClient,
    dry_run: bool = False,
    today: date | None = None,
) -> tuple[FetchStats, int]:
    """跑单个产品的一个窗口，返回 ``(统计, 提交行数)``。"""
    from newsagg.weather.client import resolve_window

    start, end = resolve_window(product, start=start, end=end, today=today)
    if start > end:
        log.info("%s 窗口为空（%s ~ %s），跳过", product, start, end)
        return FetchStats(), 0

    stats = FetchStats()
    points = client.fetch(
        product=product, stations=stations,
        start=start, end=end, today=today, stats=stats,
    )
    if dry_run:
        log.info("[dry-run] %s 将写入 %d 行，不落库", product, len(points))
        return stats, 0

    result = store.write(points)
    # 进度按「大区」切片记账：24 张表里每张都是一个独立工作单元，
    # 长回填被打断后可精确到「哪个大区哪个窗口」续跑。
    by_region: dict[str, tuple[int, int]] = {}
    for point in points:
        hourly, daily = by_region.get(point.region, (0, 0))
        if point.granularity == "hourly":
            hourly += 1
        else:
            daily += 1
        by_region[point.region] = (hourly, daily)
    for region, (hourly, daily) in by_region.items():
        store.mark_progress(
            product=product, region=region,
            period_start=start, period_end=end,
            hourly_rows=hourly, daily_rows=daily,
            stations=len({p.station_id for p in points if p.region == region}),
        )
    return stats, result.rows_submitted


def run_daily(
    *,
    store: WeatherStore,
    client: WeatherClient,
    regions: Sequence[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forecast_days: int = 7,
    dry_run: bool = False,
    today: date | None = None,
) -> dict[str, int]:
    """执行每日任务，返回 ``{产品: 提交行数}``。"""
    today = today or date.today()
    stations = store.load_stations(regions=regions)
    if not stations:
        raise RuntimeError("站点表为空——请先执行 weather-init 同步站点")

    plans = build_daily_plan(
        today=today, lookback_days=lookback_days, forecast_days=forecast_days
    )
    log.info("每日气象任务：%d 个窗口，站点 %d 个", len(plans), len(stations))
    out: dict[str, int] = {}
    for plan in plans:
        log.info("→ %s", plan.describe())
        stats, written = run_product(
            product=plan.product, stations=stations,
            start=plan.start, end=plan.end,
            store=store, client=client, dry_run=dry_run, today=today,
        )
        log.info("  %s｜写入 %d 行", stats.describe(), written)
        out[plan.product] = out.get(plan.product, 0) + written
    return out
