"""市场监管命令行：抓取（``market-fetch``）与查看（``market-stats``）。

本模块由 ``newsagg/cli.py`` 的两个子命令薄薄包一层，保持「子命令实现分散、
解析器集中」的既有约定（对比 ``cmd_schedule`` 转发到 ``newsagg.schedule``）。

为什么「抓多少期」的参数名三个采集器不一致
------------------------------------------
CLP 是**日度**数据，说「抓最近 3 期」不如说「抓最近 3 天」自然；
CECI 是**周报**、现货是**周报**，说「期」自然。硬统一成一个名字会
让至少一方的名字名不副实。所以这里用一张
:data:`_PERIOD_KWARG` 表把「期数」映射到各采集器自己的关键字：

===============  ===============
采集器            关键字
===============  ===============
``CLPFetcher``    ``days``
``PPIFetcher``    ``days``
``SHFGXFetcher``  ``days``
``CECIFetcher``   ``limit``
``GZPECFetcher``  ``limit``
===============  ===============

CLI 对外只暴露一个 ``--periods``，用户不必记这些差异。

``--dataset`` 选的是**数据集**，但要跑哪些采集器是按
:data:`~newsagg.market.fetchers.PRODUCED_DATASETS`（每个类的**全部**产出）算的。
所以 ``--dataset lng_spot`` 会同时跑 CLP、生意社、上海挂牌三家——
`lng_spot` 有三个来源，只跑登记的 `CLPFetcher` 会静默少两家。
反过来，写入时只留 ``--dataset`` 点到名的数据集：``--dataset lng_quote``
只要明细、不要汇总；两者都要就 ``--dataset all``。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from newsagg.errors import PowerNewsError
from newsagg.market.fetchers import FETCHERS, PRODUCED_DATASETS
from newsagg.market.fetchers.base import MarketFetcher
from newsagg.market.models import DATASETS, MarketPoint

__all__ = ["build_fetch_parser", "build_stats_parser", "market_fetch", "market_stats"]

#: 采集器类 → 它的 ``fetch()`` 里表示「抓多少期」的关键字名。
_PERIOD_KWARG: dict[type[MarketFetcher], str] = {}


def _period_kwarg(cls: type[MarketFetcher]) -> str:
    """查「期数」关键字，首次调用时惰性建表。

    惰性是为了避免在 import 期就依赖具体采集器类——注册表已经能让
    ``FETCHERS`` 变化，这里跟着走即可。
    """
    if not _PERIOD_KWARG:
        from newsagg.market.fetchers.ceci_cec import CECIFetcher
        from newsagg.market.fetchers.lng_100ppi import PPIFetcher
        from newsagg.market.fetchers.lng_cqpgx import CLPFetcher
        from newsagg.market.fetchers.lng_shpgx import SHFGXFetcher
        from newsagg.market.fetchers.power_gzpec import GZPECFetcher

        _PERIOD_KWARG.update(
            {
                CLPFetcher: "days",
                PPIFetcher: "days",
                SHFGXFetcher: "days",
                CECIFetcher: "limit",
                GZPECFetcher: "limit",
            }
        )
    return _PERIOD_KWARG.get(cls, "limit")


def _out(text: str = "") -> None:
    print(text)


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文表头会炸；能重配就重配。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass


def _print_json(payload: Any) -> None:
    _out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _point_payload(point: MarketPoint) -> dict[str, Any]:
    return {
        "dataset": point.dataset,
        "region": point.region,
        "grade": point.grade,
        "price_date": point.price_date.isoformat(),
        "value": point.value,
        "unit": point.unit,
        "change_value": point.change_value,
        "change_pct": point.change_pct,
        "period": point.period,
        "source": point.source,
        "source_url": point.source_url,
    }


def _resolve_datasets(dataset: str) -> list[str]:
    """把 ``--dataset`` 的值展开成数据集列表。"""
    if dataset == "all":
        return sorted(DATASETS)
    if dataset not in DATASETS:
        raise PowerNewsError(
            f"未知数据集 {dataset!r}；可选：all / {' / '.join(sorted(DATASETS))}"
        )
    return [dataset]


def _target_classes(datasets: Sequence[str]) -> list[type[MarketFetcher]]:
    """数据集 → 采集器类，并**按类去重**。

    查的是 :data:`~newsagg.market.fetchers.PRODUCED_DATASETS`（类的**全部**产出），
    不是 ``FETCHERS`` 那张 dataset → 类 的单值表。差别在 ``lng_spot``：
    它由 CLP、生意社、上海挂牌**三家**产出，只登记了 ``CLPFetcher``。
    照着登记表跑的话 ``--dataset lng_spot`` 会静默少两家——命令返回 0、
    每条日志都正常，只有翻库才发现。用产出表就不会。

    去重同样必要：``ceci_import`` 与 ``ceci_pmi`` 指向同一个 ``CECIFetcher``，
    不去重会把同一份周报抓两遍。
    """
    wanted = set(datasets)
    for name in sorted(wanted):
        if name not in FETCHERS:
            raise PowerNewsError(f"数据集 {name!r} 尚未注册采集器")

    classes: list[type[MarketFetcher]] = []
    for cls in FETCHERS.values():
        if cls in classes:
            continue
        produced = set(PRODUCED_DATASETS.get(cls.__name__, (cls.dataset,)))
        if produced & wanted:
            classes.append(cls)
    return classes


# ==========================================================================
# market-fetch
# ==========================================================================
def market_fetch(argv: Sequence[str] | None = None) -> int:
    """抓取市场行情并幂等入库。"""
    _ensure_utf8_stdout()
    args = build_fetch_parser().parse_args(list(argv) if argv is not None else None)

    datasets = _resolve_datasets(args.dataset)
    classes = _target_classes(datasets)

    # dry-run 时不导入 store，也就不需要 MySQL——可以先只验采集能不能跑通。
    store = None
    if not args.dry_run:
        from newsagg.db import Database
        from newsagg.market.store import MarketStore

        store = MarketStore(Database())

    all_points: list[MarketPoint] = []
    failures: list[str] = []

    for cls in classes:
        attempt = cls()  # type: ignore[call-arg]
        try:
            with attempt as fetcher:
                kwargs = {_period_kwarg(cls): args.periods}
                raw_points = fetcher.fetch(**kwargs)  # type: ignore[call-arg]
        except PowerNewsError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个源失败不该拖垮其它源
            failures.append(f"{cls.__name__}: {type(exc).__name__}: {exc}")
            _out(f"  ✗ {cls.__name__} 采集失败：{type(exc).__name__}: {exc}")
            continue

        # 统一按 `--dataset` 过滤。`all` 时不去重，保留采集器产出的全部数据集。
        selected = (
            raw_points
            if args.dataset == "all"
            else [p for p in raw_points if p.dataset in set(datasets)]
        )
        _out(f"  ✓ {cls.__name__:<14} 抓取 {len(raw_points):>4} 条 → 选中 {len(selected):>4} 条")
        all_points.extend(selected)

    if not all_points:
        _out("没有任何数据点。")
        if failures:
            for item in failures:
                _out(f"  ✗ {item}")
            return 1
        return 0

    if args.json:
        _print_json(
            {
                "dry_run": args.dry_run,
                "datasets": datasets,
                "points": [_point_payload(p) for p in all_points],
                "failures": failures,
            }
        )
    else:
        _out("")
        _out(f"== 抓取明细（{len(all_points)} 条）==")
        for point in sorted(all_points, key=lambda p: (p.dataset, p.price_date, p.region, p.grade)):
            _out(f"  {point.label}")

    if args.dry_run:
        _out("")
        _out("[dry-run] 未写库。")
        return 1 if failures else 0

    assert store is not None
    result = store.upsert(all_points)

    if args.json:
        _print_json(
            {
                "inserted": result.inserted,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "skipped": result.skipped,
                "total": result.total,
                "failures": failures,
            }
        )
    else:
        _out("")
        _out(f"== 入库结果 == {result.describe()}")

    return 1 if failures else 0


# ==========================================================================
# market-stats
# ==========================================================================
def market_stats(argv: Sequence[str] | None = None) -> int:
    """查看 ``market_prices`` 的覆盖情况与最新数据。"""
    _ensure_utf8_stdout()
    args = build_stats_parser().parse_args(list(argv) if argv is not None else None)

    from newsagg.db import Database
    from newsagg.market.store import MarketStore

    store = MarketStore(Database())

    if args.latest:
        dataset = None if args.dataset == "all" else args.dataset
        rows = store.latest(dataset=dataset, limit=args.latest)
        if args.json:
            _print_json(rows)
            return 0
        if not rows:
            _out("market_prices 还没有数据——先跑 `powernews market-fetch`。")
            return 1
        _out(f"最新 {len(rows)} 条：")
        _out(f"  {'数据集':<13}{'区域':<10}{'规格':<16}{'日期':<12}{'数值':>10} {'单位':<10}{'环比':>8}  期次")
        for row in rows:
            value = "—" if row.get("value") is None else f"{float(row['value']):g}"
            change = "—" if row.get("change_value") is None else f"{float(row['change_value']):+g}"
            _out(
                f"  {str(row.get('dataset')):<13}{str(row.get('region')):<10}"
                f"{str(row.get('grade')):<16}{str(row.get('price_date')):<12}"
                f"{value:>10} {str(row.get('unit') or ''):<10}{change:>8}  {row.get('period') or ''}"
            )
        return 0

    rows = store.summary()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        _out("market_prices 还没有数据——先跑 `powernews market-fetch`。")
        return 1

    _out("== market_prices 覆盖情况 ==")
    _out(f"  {'数据集':<13}{'含义':<26}{'行数':>6}{'区域':>6}{'来源':>6}  {'日期区间':<24}来源")
    for row in rows:
        span = f"{row.get('date_from')} ~ {row.get('date_to')}"
        _out(
            f"  {str(row.get('dataset')):<13}{str(row.get('dataset_name')):<26}"
            f"{int(row.get('rows_total') or 0):>6}{int(row.get('regions') or 0):>6}"
            f"{int(row.get('sources') or 0):>6}  {span:<24}{row.get('source_list') or ''}"
        )
    _out("")
    _out(f"  合计 {sum(int(r.get('rows_total') or 0) for r in rows)} 行")
    return 0


# ==========================================================================
# 解析器
# ==========================================================================
def build_fetch_parser() -> argparse.ArgumentParser:
    """``market-fetch`` 的参数。"""
    parser = argparse.ArgumentParser(
        prog="powernews market-fetch",
        description="抓取市场行情（LNG 现价 / CECI 电煤指数 / 南方区域电力现货）并幂等入库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  powernews market-fetch --dry-run\n"
            "  powernews market-fetch --dataset power_spot --periods 4\n"
            "  powernews market-fetch --dataset all --periods 3\n"
        ),
    )
    parser.add_argument(
        "--dataset",
        default="all",
        choices=["all", *sorted(DATASETS)],
        help="要抓的数据集（默认 all）",
    )
    parser.add_argument(
        "--periods",
        type=int,
        default=1,
        help="抓最近多少期（LNG 按天、CECI/现货按周；默认 1）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只抓不写库（不需要 MySQL）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser


def build_stats_parser() -> argparse.ArgumentParser:
    """``market-stats`` 的参数。"""
    parser = argparse.ArgumentParser(
        prog="powernews market-stats",
        description="查看 market_prices 的覆盖情况与最新数据",
    )
    parser.add_argument(
        "--dataset",
        default="all",
        choices=["all", *sorted(DATASETS)],
        help="只看某个数据集（默认 all）",
    )
    parser.add_argument("--latest", type=int, default=0, metavar="N",
                        help="改为列出最新 N 条明细（默认 0 = 输出覆盖汇总）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser
