"""市场行情采集端到端验证（真实网络 + 真实 MySQL）。

与 ``scripts/e2e_smoke.py`` 的分工
----------------------------------
``e2e_smoke.py`` 是**纯 HTTP** 的中台冒烟（只 import httpx），它验的是「上线形态
的接口」。市场行情走的是另一条链路——采集器直连对方站点、再写采集库
``powernews.market_prices``，**不经过中台**。两条链路的失败模式完全不同
（接口挂 vs 站点改版 / 库写不进去），所以分开放，各自的脚本保持单一职责。

这个脚本验什么
--------------
1. **五个采集器能不能真的从网上抓到**（CLP / 生意社 / SHFGX / CECI / 南方现货）；
2. **抓到的数据在语义上合不合理**（日期不落在未来、涨跌幅量级不离谱、
   唯一键无重复、单位一致）；
3. **幂等**：同一批数据连写两次，第二次必须是「未变」而不是重复插入；
4. **可读回**：``market-stats`` 用的那些查询确实能取到刚写进去的数据。

用法::

    python scripts/verify_market.py                 # 抓 1 期，联网 + 写库
    python scripts/verify_market.py --periods 4     # 各抓 4 期
    python scripts/verify_market.py --offline       # 跳过联网，只验库里已有数据

退出码：0 = 全绿；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsagg.market.fetchers import FETCHERS  # noqa: E402
from newsagg.market.fetchers.ceci_cec import CECIFetcher  # noqa: E402
from newsagg.market.fetchers.lng_100ppi import PPIFetcher  # noqa: E402
from newsagg.market.fetchers.lng_cqpgx import CLPFetcher  # noqa: E402
from newsagg.market.fetchers.lng_shpgx import (  # noqa: E402
    AGGREGATES,
    SHFGXFetcher,
    is_receive_station,
)
from newsagg.market.fetchers.power_gzpec import GZPECFetcher  # noqa: E402
from newsagg.market.models import DATASETS, SIGNED_DATASETS, MarketPoint  # noqa: E402
from newsagg.market.store import MarketStore  # noqa: E402

_PASS = 0
_FAIL = 0


def _check(name: str, cond: bool, extra: str = "") -> bool:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))
    return bool(cond)


def _section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def _sanity(points: list[MarketPoint], label: str) -> bool:
    """对一批数据点做与来源无关的通用合理性检查。

    这些断言的价值在于：它们是**跨数据集不变式**。任何一次「解析器悄悄错位」
    （比如把电量当价格、把日期整体偏移一周）都会在这里露头，
    而不必逐源写一套专门的校验。

    ``lng_signal`` 例外：它是价差（10日均线 − 20日均线），**天然有负值**，
    所以「非负」这条对它跳过。这正是 :data:`SIGNED_DATASETS` 存在的意义——
    把「哪些数据集可以取负」写成显式事实，而不是散落在几句 ``if`` 里。
    """
    ok = True
    ok &= _check(f"{label} 非空", bool(points), f"{len(points)} 条")

    keys = [p.key for p in points]
    ok &= _check(
        f"{label} 唯一键无重复",
        len(set(keys)) == len(keys),
        f"{len(set(keys))}/{len(keys)}",
    )

    today = date.today()
    ok &= _check(
        f"{label} 业务日期不落在未来",
        all(p.price_date <= today + timedelta(days=1) for p in points),
        f"最新 {max(p.price_date for p in points)}",
    )

    priced = [p for p in points if p.value is not None]
    datasets = {p.dataset for p in points}
    if datasets <= SIGNED_DATASETS:
        print(f"  [skip] {label} 数值非负（{sorted(datasets)} 是允许取负的信号数据集）")
    else:
        ok &= _check(f"{label} 数值非负", all(p.value >= 0 for p in priced), f"{len(priced)} 条有值")
    ok &= _check(
        f"{label} 数值量级合理（< 1e6）",
        all(abs(p.value) < 1e6 for p in priced),
    )
    return ok


def _period_kwargs(cls: type, periods: int) -> dict[str, int]:
    """把「期数」映射到各采集器自己的关键字（与 ``newsagg/market/cli.py`` 一致）。"""
    if cls in (CLPFetcher, PPIFetcher, SHFGXFetcher):
        return {"days": periods}
    return {"limit": periods}


def _shpgx_reconcile(quotes: list[MarketPoint], summary: list[MarketPoint]) -> tuple[bool, str]:
    """明细与汇总必须对得上——这是「两个数据集同源」设计的验收。

    汇总口径（均价 / 最高 / 最低）完全由明细推导，所以拿明细在本脚本里**独立重算
    一遍**，逐日逐口径比对。只要汇总的分组键或日期挂错，这里立刻暴露；
    而单看汇总数字本身是看不出问题的（每个数都"合理"）。

    Returns:
        ``(是否一致, 差异说明)``
    """
    by_date: dict[date, list[MarketPoint]] = defaultdict(list)
    for point in quotes:
        by_date[point.price_date].append(point)

    got = {(p.price_date, p.grade): p.value for p in summary}
    if not got:
        return False, "汇总为空"

    if {d for d, _ in got} != set(by_date):
        return False, f"日期集合不一致：汇总 {sorted({d for d, _ in got})} vs 明细 {sorted(by_date)}"

    for day, items in by_date.items():
        all_values = [float(p.value) for p in items if p.value is not None]
        rec_values = [
            float(p.value) for p in items if p.value is not None and is_receive_station(p.grade)
        ]
        expected: dict[str, float] = {
            "挂牌·全部均价": round(statistics.mean(all_values), 2),
            "挂牌·全部最高": round(max(all_values), 2),
            "挂牌·全部最低": round(min(all_values), 2),
        }
        if rec_values:
            expected.update(
                {
                    "挂牌·接收站均价": round(statistics.mean(rec_values), 2),
                    "挂牌·接收站最高": round(max(rec_values), 2),
                    "挂牌·接收站最低": round(min(rec_values), 2),
                }
            )
        for grade, value in expected.items():
            if got.get((day, grade)) != value:
                return False, f"{day} {grade}：汇总 {got.get((day, grade))} ≠ 重算 {value}"

    return True, f"{len(by_date)} 天 × {len({g for _, g in got})} 口径全部一致"


def _quote_interval_ok(quotes: list[MarketPoint]) -> bool:
    """检查企业报价的「最低 ≤ 均价 ≤ 最高」在每一天都成立。

    这条不变式能抓住一类很隐蔽的错位：把不同日期的最高/最低/均价拼错了行。
    单独看每个数字都「合理」，只有跨 grade 对比才露馅。
    """
    if not quotes:
        return False
    by_date: dict[date, dict[str, float]] = {}
    for point in quotes:
        by_date.setdefault(point.price_date, {})[point.grade] = point.value
    for grades in by_date.values():
        low = grades.get("企业报价·最低")
        avg = grades.get("企业报价·均价")
        high = grades.get("企业报价·最高")
        if None in (low, avg, high) or not (low <= avg <= high):
            return False
    return all(len(g) == 3 for g in by_date.values())


# ----------------------------------------------------------------------
# 1. 采集器联网验证
# ----------------------------------------------------------------------
def verify_collectors(periods: int) -> dict[str, list[MarketPoint]]:
    _section("1. 采集器联网验证")
    collected: dict[str, list[MarketPoint]] = {}

    # ---- 1a. CLP：两段式站点 ----
    try:
        with CLPFetcher() as fetcher:
            dates = fetcher.available_dates()
        _check("CLP 日期列表页可解析", bool(dates), f"{len(dates)} 个发布日期，最新 {dates[0]}")
        with CLPFetcher() as fetcher:
            points = fetcher.fetch()
        _check("CLP fetch() 拿到数据", bool(points), f"{len(points)} 条")
        _check(
            "CLP 含全国口径（映射为 CN）",
            any(p.region == "CN" for p in points),
        )
        _check(
            "CLP 区域数 >= 20",
            len({p.region for p in points}) >= 20,
            f"{len({p.region for p in points})} 个区域",
        )
        _check("CLP 单位统一 元/吨", {p.unit for p in points} == {"元/吨"})
        collected["lng_spot"] = points
    except Exception as exc:  # noqa: BLE001 - 单源失败不该中断其余验证
        _check("CLP 采集", False, f"{type(exc).__name__}: {exc}")

    # ---- 1b. 生意社：国内 LNG 基准价 + 企业报价区间 + 均差 ----
    try:
        with PPIFetcher() as fetcher:
            points = fetcher.fetch(days=max(1, periods))
        benchmarks = [p for p in points if p.grade == "生意社基准价"]
        quotes = [p for p in points if p.grade.startswith("企业报价")]
        signals = [p for p in points if p.dataset == "lng_signal"]

        _check("生意社安全检查已自动通过（返回的是真实页面）", bool(points), f"{len(points)} 条")
        _check("生意社基准价可解析", bool(benchmarks), f"{len(benchmarks)} 天")
        _check(
            "生意社基准价量级合理（1000~15000 元/吨）",
            all(1000 <= p.value <= 15000 for p in benchmarks),
            f"{min(p.value for p in benchmarks):g}~{max(p.value for p in benchmarks):g}",
        )
        _check(
            "企业报价区间自洽（最低 ≤ 均价 ≤ 最高）",
            _quote_interval_ok(quotes),
            f"{len({p.price_date for p in quotes})} 天 × 3 档",
        )
        _check(
            "均差信号允许取负且有样本",
            bool(signals),
            f"{len(signals)} 条，含负值 {sum(1 for p in signals if p.value < 0)} 条",
        )
        collected["lng_signal"] = signals
    except Exception as exc:  # noqa: BLE001
        _check("生意社采集", False, f"{type(exc).__name__}: {exc}")

    # ---- 1b. CECI：观测周结束日必须早于发布日 ----
    try:
        with CECIFetcher() as fetcher:
            reports = fetcher.list_reports(limit=periods)
            points = fetcher.fetch(limit=periods)
        _check("CECI 周报列表可解析", bool(reports), f"{len(reports)} 期")
        _check("CECI 抓到数据", bool(points), f"{len(points)} 条")
        _check(
            "CECI 期次格式正确",
            all(p.period.startswith("20") and "年第" in p.period for p in points),
            f"最新 {max(points, key=lambda p: p.price_date).period}",
        )
        import_values = [p.value for p in points if p.dataset == "ceci_import"]
        _check(
            "CECI 进口指数量级合理（300~5000 元/吨）",
            all(300 <= v <= 5000 for v in import_values),
            f"{len(import_values)} 个值，范围 {min(import_values) if import_values else '-'}"
            f"~{max(import_values) if import_values else '-'}",
        )
        pmi_values = [p.value for p in points if p.dataset == "ceci_pmi"]
        _check("CECI PMI 在 0~100 区间", all(0 < v < 100 for v in pmi_values), f"{len(pmi_values)} 个值")
        collected["ceci_import"] = points
    except Exception as exc:  # noqa: BLE001
        _check("CECI 采集", False, f"{type(exc).__name__}: {exc}")

    # ---- 1c. 南方区域现货 ----
    try:
        with GZPECFetcher() as fetcher:
            reports = fetcher.list_reports(limit=periods)
            points = fetcher.fetch(limit=periods)
        _check("现货周报列表可解析", bool(reports), f"{len(reports)} 期")
        _check(
            "现货周报标题过滤生效（只留现货，不含绿证/月报）",
            all("电力现货市场每周行情一览" in r["title"] for r in reports),
        )
        _check("现货抓到数据", bool(points), f"{len(points)} 条")
        _check(
            "现货三层齐全（区域整体/分省区/分电源）",
            {"日前", "实时"} <= {p.grade for p in points}
            and any("·" in p.grade for p in points)
            and {"广东", "广西", "云南", "贵州", "海南"} <= {p.region for p in points},
            f"{len({p.region for p in points})} 个区域 / {len({p.grade for p in points})} 种规格",
        )
        _check("现货单位统一 元/兆瓦时", {p.unit for p in points} == {"元/兆瓦时"})
        spot_values = [p.value for p in points]
        _check(
            "现货均价量级合理（50~3000 元/兆瓦时）",
            all(50 <= v <= 3000 for v in spot_values),
            f"范围 {min(spot_values)}~{max(spot_values)}",
        )
        collected["power_spot"] = points
    except Exception as exc:  # noqa: BLE001
        _check("现货采集", False, f"{type(exc).__name__}: {exc}")

    # ---- 1d. 上海石油天然气交易中心：挂牌明细 + 汇总 ----
    try:
        with SHFGXFetcher() as fetcher:
            points = fetcher.fetch(days=max(1, periods))
        quotes = [p for p in points if p.dataset == "lng_quote"]
        summary = [p for p in points if p.dataset == "lng_spot"]

        _check("SHFGX 挂牌明细可解析", bool(quotes), f"{len(quotes)} 条")
        _check(
            "SHFGX 销售地覆盖多个省份/大区",
            len({p.region for p in quotes}) >= 3,
            f"{len({p.region for p in quotes})} 个：{sorted({p.region for p in quotes})[:6]}",
        )

        facilities = {p.grade for p in quotes}
        receive = sorted(f for f in facilities if is_receive_station(f))
        _check(
            "SHFGX 认得出接收站（含不带「接收站」后缀的北燃南港）",
            len(receive) >= 2,
            f"接收站 {len(receive)} 个 / 交货点 {len(facilities)} 个",
        )
        _check(
            "SHFGX 报价落到 5000~8000 元/吨（含山西液厂低价与宁波接收站高价）",
            all(5000 <= p.value <= 8000 for p in quotes),
            f"{min(p.value for p in quotes):g}~{max(p.value for p in quotes):g}",
        )

        _check(
            "SHFGX 汇总口径齐全",
            {p.grade for p in summary} == {g for g, _, _ in AGGREGATES},
            f"{len({p.grade for p in summary})} 种 × {len({p.price_date for p in summary})} 天",
        )

        reconciled, detail = _shpgx_reconcile(quotes, summary)
        _check("SHFGX 汇总可由明细独立重算（明细与汇总同源）", reconciled, detail)

        # 「接收站」是「全部」的子集，所以极值必须被包含——纯结构不变式，
        # 与归约公式无关，能挡住"两个口径取错了样本"这类错
        _check("SHFGX 接收站极值被全部极值包住", _shpgx_containment_ok(summary))

        collected["lng_quote"] = points
    except Exception as exc:  # noqa: BLE001
        _check("SHFGX 采集", False, f"{type(exc).__name__}: {exc}")

    return collected


def _shpgx_containment_ok(summary: list[MarketPoint]) -> bool:
    """接收站极值必须被「全部」极值包住（接收站样本 ⊆ 全部样本）。

    纯结构不变式，与归约公式无关：只要两个口径取错了样本就会露馅。
    """
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for point in summary:
        by_day[point.price_date][point.grade] = point.value

    for grades in by_day.values():
        all_low, all_high = grades.get("挂牌·全部最低"), grades.get("挂牌·全部最高")
        rec_low, rec_high = grades.get("挂牌·接收站最低"), grades.get("挂牌·接收站最高")
        if None in (all_low, all_high, rec_low, rec_high):
            return False
        if not (all_low <= rec_low <= rec_high <= all_high):
            return False
    return bool(by_day)


# ----------------------------------------------------------------------
# 2. 入库与幂等
# ----------------------------------------------------------------------
def verify_store(points: list[MarketPoint]) -> None:
    _section("2. 入库与幂等")
    if not points:
        _check("入库验证", False, "上一步没有拿到任何数据点")
        return

    store = MarketStore()

    first = store.upsert(points)
    _check(
        "首次入库不报错",
        first.total == len(points),
        f"{first.describe()}",
    )

    second = store.upsert(points)
    _check(
        "二次入库全部「未变」（幂等）",
        second.inserted == 0 and second.updated == 0 and second.unchanged == len(points),
        f"{second.describe()}",
    )

    # 篡改一个值再写回 → 应当被判为「更新」，且行数不增。
    # 验完必须**原样写回**：否则每跑一次这个脚本，库里那条数据就被 +1，
    # 跑十次之后谁也说不清为什么指数对不上。
    original = points[0]
    mutated = MarketPoint(
        dataset=original.dataset,
        region=original.region,
        grade=original.grade,
        price_date=original.price_date,
        value=(original.value or 0) + 1,
        unit=original.unit,
        source=original.source,
    )
    before = store.count()
    third = store.upsert([mutated])
    after = store.count()
    _check(
        "值变化时判为「更新」且不新增行",
        third.updated == 1 and third.inserted == 0 and before == after,
        f"{third.describe()}，行数 {before} → {after}",
    )

    restored = store.upsert([original])
    _check(
        "变异测试后已还原原值",
        restored.updated == 1 and restored.inserted == 0,
        f"{original.label} ← {restored.describe()}",
    )


# ----------------------------------------------------------------------
# 3. 库内数据可读回
# ----------------------------------------------------------------------
def verify_readback() -> None:
    _section("3. 库内数据可读回")
    store = MarketStore()

    total = store.count()
    _check("market_prices 有数据", total > 0, f"{total} 行")

    summary = store.summary()
    present = {row["dataset"] for row in summary}
    _check(
        "各采集模块的数据集都已落库",
        {"lng_spot", "lng_quote", "lng_signal", "ceci_import", "power_spot"} <= present,
        f"实际 {sorted(present)}",
    )
    _check(
        "所有 dataset 都在 DATASETS 常量里登记",
        present <= set(DATASETS),
        f"未登记：{sorted(present - set(DATASETS))}",
    )
    _check(
        "可为负的数据集已显式登记（避免被当成价格参与均值）",
        SIGNED_DATASETS <= set(DATASETS),
        f"{sorted(SIGNED_DATASETS)}",
    )

    print()
    print(f"  {'数据集':<13}{'含义':<26}{'行数':>6}{'区域':>6}  日期区间")
    for row in summary:
        span = f"{row.get('date_from')} ~ {row.get('date_to')}"
        print(
            f"  {str(row.get('dataset')):<13}{str(row.get('dataset_name')):<26}"
            f"{int(row.get('rows_total') or 0):>6}{int(row.get('regions') or 0):>6}  {span}"
        )

    latest = store.latest(limit=5)
    _check("latest() 可读", bool(latest), f"{len(latest)} 条")
    _check(
        "latest() 按业务日期倒序",
        all(
            latest[i]["price_date"] >= latest[i + 1]["price_date"]
            for i in range(len(latest) - 1)
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="市场行情采集端到端验证")
    parser.add_argument("--periods", type=int, default=1, help="每个采集器抓多少期（默认 1）")
    parser.add_argument("--offline", action="store_true", help="跳过联网，只验库里已有数据")
    args = parser.parse_args()

    print("市场行情采集端到端验证")
    print(f"  采集器注册表：{len(set(FETCHERS.values()))} 个类 / {len(FETCHERS)} 个数据集键")
    print(f"  数据集：{', '.join(sorted(DATASETS))}")

    points: list[MarketPoint] = []
    if not args.offline:
        collected = verify_collectors(args.periods)
        for batch in collected.values():
            points.extend(batch)
        if points:
            _section("1b. 采集结果合理性（跨数据集不变式）")
            for name, batch in collected.items():
                _sanity(batch, name)
    else:
        _section("1. 采集器联网验证")
        print("  (--offline：已跳过)")

    if points:
        verify_store(points)
    elif not args.offline:
        _section("2. 入库与幂等")
        _check("入库验证", False, "没有可入库的数据点")

    verify_readback()

    _section("汇总")
    print(f"  PASS {_PASS} / FAIL {_FAIL}")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
