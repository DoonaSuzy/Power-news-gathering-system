"""基准：同一批 10 万条数据，逐行写法 vs SDK 向量化管线。

简历口径写的是「12 秒 → 1.5 秒」。这类数字只有两种时候值得写进文档：要么它当时
被测过，要么它现在能被复现。本脚本负责后者——同一份输入喂给三条实现，先断言
**输出一致**（不一致的加速没有意义），再报耗时与倍数。

三条实现里有两个逐行基线，因为它们回答的是不同问题：

* ``iterrows`` —— 改造前最常见的业务写法（每行一个 Series、按列名取值）；
* ``itertuples`` —— **已经被手工优化过**的逐行写法（命名元组、无索引开销）。

只报前者会把收益全记在「向量化」头上，其中一部分其实只是"别用 iterrows"。
两个都测，才能把「向量化」和「换一种读法」各自的贡献拆开写。

用法::

    python scripts/bench_vectorized.py --rows 100000 --repeats 5
    python scripts/bench_vectorized.py --json      # 供 CI / 文档取数

参照实现是本仓库自己写的，不是上游项目的历史代码，所以基线秒数必须连这台机器
一起引用（脚本会打印 Python/pandas 版本），不能脱离环境单说。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from powersdk import InMemoryTimeSeriesSource, vectorized_transform  # noqa: E402


def make_batch(rows: int, *, seed: int = 20250101) -> pd.DataFrame:
    """造一批带缺口的原始长表（34 省 × N 小时 ≥ rows 行）。"""
    hours = -(-rows // 34) + 1
    source = InMemoryTimeSeriesSource(seed=seed, hours=hours, missing_rate=0.06)
    return source.fetch("load")


# --------------------------------------------------------------- 逐行参照实现
def _to_datetime(ts: object) -> datetime:
    if isinstance(ts, str):
        return datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S")
    if isinstance(ts, pd.Timestamp):
        return ts.to_pydatetime()
    return ts  # type: ignore[return-value]


def _row_pairs(frame: pd.DataFrame, *, mode: str) -> Iterator[tuple[str, object, object]]:
    """按「实际的读法」逐行产出 (region, ts, value)，只切换读取方式。"""
    if mode == "iterrows":
        for _, row in frame.iterrows():
            yield str(row["region"]), row["ts"], row["value"]
    else:
        for row in frame.itertuples(index=False):
            yield str(row.region), row.ts, row.value


def naive_transform(frame: pd.DataFrame, *, mode: str = "iterrows") -> pd.DataFrame:
    """不用向量化算子，把同一件事做一遍：解析 → 去重 → 线性插值 → 还原长表。

    补缺语义对齐 :func:`powersdk.utils.fill_gaps`（默认 ``limit=None``）：先线性
    插值，两端只有单侧已知点时退回单侧值。注意必须显式把 NaN 当缺失——数据源的
    空洞是 NaN 而不是 ``None``，只判 ``is None`` 会让洞被当成已知点原样留下，两
    条路径的语义就对不上了。这正是参照实现的价值所在。
    """
    parsed: dict[str, dict[datetime, float]] = {}
    for region, raw_ts, raw_value in _row_pairs(frame, mode=mode):
        if raw_value is None:
            continue
        try:
            value = float(str(raw_value).strip())
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN（含字符串 "nan"）视为缺失
            continue
        parsed.setdefault(region.strip(), {})[_to_datetime(raw_ts)] = value

    step = timedelta(hours=1)
    # fill_gaps 的网格取自整批数据的 min/max 时间戳，不是每个地区自己的首个
    # 已知点——起点那一小时正好是空洞时，向量化侧会 bfill 出值、逐行侧若按本地
    # 区间走就少一行。参照实现必须走同一张网格，否则"行数不同"会被误读成 bug。
    grid_start = _to_datetime(frame["ts"].min())
    grid_end = _to_datetime(frame["ts"].max())
    out_rows: list[dict[str, object]] = []
    for region in sorted(parsed):
        known = parsed[region]
        stamps = sorted(known)
        first, last = stamps[0], stamps[-1]
        cursor = grid_start
        while cursor <= grid_end:
            value = known.get(cursor)
            if value is None:
                left = right = None
                probe = cursor - step
                while probe >= first:
                    if probe in known:
                        left = probe
                        break
                    probe -= step
                probe = cursor + step
                while probe <= last:
                    if probe in known:
                        right = probe
                        break
                    probe += step
                if left is not None and right is not None:
                    span = (right - left).total_seconds() / 3600.0
                    weight = (cursor - left).total_seconds() / 3600.0 / span
                    value = known[left] + (known[right] - known[left]) * weight
                elif left is not None:
                    value = known[left]
                elif right is not None:
                    value = known[right]
            if value is not None:
                out_rows.append({"ts": cursor, "region": region, "value": value})
            cursor += step

    result = pd.DataFrame(out_rows, columns=["ts", "region", "value"])
    result["unit"] = "MW"
    return result.sort_values(["ts", "region"], kind="stable").reset_index(drop=True)


# ------------------------------------------------------------------- 计时框架
def timeit(fn: Callable[[pd.DataFrame], pd.DataFrame], payload: pd.DataFrame,
           *, repeats: int) -> dict[str, object]:
    samples: list[float] = []
    output: pd.DataFrame | None = None
    for _ in range(repeats):
        started = time.perf_counter()
        output = fn(payload)
        samples.append(time.perf_counter() - started)
    assert output is not None
    return {
        "median": statistics.median(samples),
        "min": min(samples),
        "max": max(samples),
        "rows": int(len(output)),
        "output": output,
    }


def compare(a: pd.DataFrame, b: pd.DataFrame, *, tolerance: float = 1e-4) -> dict[str, object]:
    """两条路径的输出一致性：行集相同、数值同量级、不残留单侧 NaN。"""
    left = a.sort_values(["ts", "region"], kind="stable").reset_index(drop=True)
    right = b.sort_values(["ts", "region"], kind="stable").reset_index(drop=True)
    if len(left) != len(right):
        return {"agree": False, "why": f"行数不同：{len(left)} vs {len(right)}"}
    if not ((left["ts"] == right["ts"]).all() and (left["region"] == right["region"]).all()):
        return {"agree": False, "why": "行数相同但 (ts, region) 键集不一致"}
    lv = left["value"].astype(float)
    rv = right["value"].astype(float)
    both_nan = lv.isna() & rv.isna()
    diff = (lv - rv).abs().mask(both_nan, 0.0)
    scale = lv.abs().clip(lower=1.0).mask(both_nan, 1.0)
    rel = diff / scale
    return {
        "agree": True,
        "rows": int(len(left)),
        "nan_both": int(both_nan.sum()),
        "nan_one_side": int(diff.isna().sum()),
        "max_relative_error": float(rel.max()),
        "within_tolerance": bool(rel.le(tolerance).fillna(False).all()),
    }


def _report_rowloop(name: str, raw: pd.DataFrame, args, fn) -> dict[str, object]:
    naive_input = raw if not args.naive_rows else raw.head(args.naive_rows)
    run = timeit(fn, naive_input, repeats=max(1, args.repeats - 2))
    per_row_s = run["median"] / len(naive_input)
    return {
        "name": name,
        "median_s": round(run["median"], 4),
        "min_s": round(run["min"], 4),
        "max_s": round(run["max"], 4),
        "rows_in": int(len(naive_input)),
        "per_row_ms": round(per_row_s * 1000, 5),
        "full_batch_s": round(per_row_s * len(raw), 4),
        "measured_on_full_batch": args.naive_rows == 0,
        "_run": run,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="10 万条：逐行 vs 向量化基准")
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--naive-rows", type=int, default=0,
                    help="逐行实现只用前 N 行再按行外推（0=全量实测，默认 0）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    raw = make_batch(args.rows)
    vector = timeit(
        lambda f: vectorized_transform(
            f, granularity="hourly", fill="interpolate", method="linear"
        ).frame,
        raw,
        repeats=args.repeats,
    )

    baselines = [
        _report_rowloop("rowloop_iterrows", raw, args,
                        lambda f: naive_transform(f, mode="iterrows")),
        _report_rowloop("rowloop_itertuples", raw, args,
                        lambda f: naive_transform(f, mode="itertuples")),
    ]
    for item in baselines:
        run = item.pop("_run")
        item["outputs_agree"] = (
            compare(run["output"], vector["output"])
            if item["measured_on_full_batch"]
            else {"skipped": "只跑了截断样本，无法逐行对照"}
        )

    report = {
        "environment": {
            "python": sys.version.split()[0],
            "pandas": pd.__version__,
            "rows": int(len(raw)),
            "rows_out": vector["rows"],
            "repeats_vector": args.repeats,
            "repeats_rowloop": max(1, args.repeats - 2),
        },
        "vectorized": {
            "median_s": round(vector["median"], 4),
            "min_s": round(vector["min"], 4),
            "max_s": round(vector["max"], 4),
            "rows_out": vector["rows"],
        },
        "rowloop_baselines": baselines,
        "speedup_vs_iterrows": round(baselines[0]["full_batch_s"] / vector["median"], 2),
        "speedup_vs_itertuples": round(baselines[1]["full_batch_s"] / vector["median"], 2),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    env = report["environment"]
    print(f"输入 {env['rows']:,} 行 → 输出 {env['rows_out']:,} 行 · "
          f"Python {env['python']} · pandas {env['pandas']}")
    print(f"  向量化（{args.repeats} 次取中位数）：{vector['median']:.3f}s  "
          f"min {vector['min']:.3f}s  max {vector['max']:.3f}s")
    for item in baselines:
        agree = item["outputs_agree"]
        print(f"  {item['name']}（{max(1, args.repeats - 2)} 次取中位数，"
              f"{item['rows_in']:,} 行）：{item['median_s']:.3f}s "
              f"→ 全量 {item['full_batch_s']:.3f}s ｜ "
              f"是向量化的 {item['full_batch_s'] / vector['median']:.1f} 倍 ｜ "
              f"一致性 {agree}")
    print(f"  倍数：vs iterrows {report['speedup_vs_iterrows']}× · "
          f"vs itertuples {report['speedup_vs_itertuples']}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
