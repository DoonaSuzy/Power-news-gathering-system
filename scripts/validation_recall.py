"""校验机制的检出率：往干净数据里注入已知脏行，看能不能抓到、有没有冤枉好行。

「准确率 100%」单独看没有价值——区间画得足够宽就能量产 100%。这份脚本反着量：
注入固定数量、**互不重叠**的已知脏行（空值、上/下越界、重复键、时间倒退，外加
一列不扣准确率的单位问题），再看校验器抓到几行。检出率（recall）+ 干净数据上的
误报数，才是「内置数据校验与告警机制」的凭据；真实数据上的准确率由
``accuracy_audit.py`` 量，这里证明那个指标不是空的。

数据源跟着环境走（``POWERSDK_API_BASE_URL`` 有值就打中台，否则用离线内存源），
所以 CI 上不联网也能跑。

用法::

    python scripts/validation_recall.py                          # 离线
    POWERSDK_API_BASE_URL=http://192.168.101.84:8080/api/v1 \
      POWERSDK_API_TOKEN=... python scripts/validation_recall.py \
      --category weather --param wind_speed_10m
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from powersdk import PowerDataClient, default_source, spec_for_category, validate  # noqa: E402

#: 每类脏行注入几条。``unit_mismatch`` 是 warning、不扣准确率，所以不进 recall。
INJECTIONS: dict[str, int] = {
    "null_value": 3,
    "out_of_range_low": 2,
    "out_of_range_high": 2,
    "duplicate_key": 2,
    "non_monotonic": 1,
    "unit_mismatch": 2,
}
ERROR_CLASSES = tuple(k for k in INJECTIONS if k != "unit_mismatch")


def inject(frame: pd.DataFrame, spec) -> pd.DataFrame:
    """在**同一地区**的连续序列上制造互不重叠的脏行（行号从 0 起算）。"""
    dirty = frame.copy()
    low = spec.value_min if spec.value_min is not None else 0.0
    high = spec.value_max if spec.value_max is not None else 1e9

    cursor = 0
    for _ in range(INJECTIONS["null_value"]):
        dirty.loc[cursor, "value"] = None
        cursor += 1
    for _ in range(INJECTIONS["out_of_range_low"]):
        dirty.loc[cursor, "value"] = low - 10_000.0
        cursor += 1
    for _ in range(INJECTIONS["out_of_range_high"]):
        dirty.loc[cursor, "value"] = high + 10_000.0
        cursor += 1
    for _ in range(INJECTIONS["unit_mismatch"]):
        dirty.loc[cursor, "unit"] = "华氏度"
        cursor += 1

    # 时间倒退：把第 10 行挪到整段序列之前。只挪一小时会正好撞上第 8 行的时间戳，
    # 一行同时算「倒退 + 重复」，脏行就串类了，逐码计数不再等于注入条数。
    dirty.loc[10, "ts"] = dirty.loc[0, "ts"] - pd.Timedelta(hours=1)

    # 重复键：复制**末行**两次。复制中间的行会在到达顺序上顺手制造一次倒退，
    # 那样脏行就串了类，recall 不再是「一类一条」。
    last = len(dirty) - 1
    dirty = pd.concat([dirty, dirty.iloc[[last]], dirty.iloc[[last]]], ignore_index=True)
    return dirty


def build_case(
    category: str, param: str | None, region: str, rows: int,
    start: object = None, end: object = None,
) -> dict[str, object]:
    """取一段干净数据 → 基线校验 → 注入脏行 → 复校，返回两侧的数字。"""
    client = PowerDataClient(source=default_source(), env="test", fill=True, clean=False)
    frame = client.query(
        category, region=region, start=start, end=end, granularity="hourly", param=param
    ).head(rows)
    if len(frame) < rows:
        raise SystemExit(f"只取到 {len(frame)} 行，不够注入 {rows} 行干净数据")
    frame = frame.reset_index(drop=True)

    spec = spec_for_category(category, param)
    baseline = validate(frame, spec)
    report = validate(inject(frame, spec), spec)

    injected = sum(INJECTIONS[k] for k in ERROR_CLASSES)
    caught_errors = sum(v.count for v in report.errors)
    return {
        "category": category,
        "param": param,
        "region": region,
        "rule": spec.name,
        "bounds": [spec.value_min, spec.value_max],
        "unit": spec.unit,
        "clean_rows": len(frame),
        "clean_accuracy": round(baseline.accuracy, 6),
        "clean_false_positives": len(frame) - baseline.valid_rows,
        "clean_error_codes": sorted(v.code for v in baseline.errors),
        "injected_error_rows": injected,
        "injected_warning_rows": INJECTIONS["unit_mismatch"],
        "per_code": {v.code: v.count for v in report.violations},
        "caught_error_rows": caught_errors,
        "recall": round(caught_errors / injected, 4),
        "dirty_accuracy": round(report.accuracy, 6),
        "below_threshold": not report.passed(0.998),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验机制检出率实测")
    ap.add_argument("--rows", type=int, default=200, help="干净数据行数")
    ap.add_argument("--category", default="load")
    ap.add_argument("--param", default=None)
    ap.add_argument("--region", default="广东")
    ap.add_argument("--start", default=None, help="实时数据源建议给个小窗口，别把整张表拉回来")
    ap.add_argument("--end", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = build_case(args.category, args.param, args.region, args.rows, args.start, args.end)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"规则 {result['rule']} 区间 [{result['bounds'][0]}, {result['bounds'][1]}] "
          f"单位 {result['unit']} · 地区 {result['region']}")
    print(f"  干净 {result['clean_rows']} 行：准确率 {result['clean_accuracy']:.4%} · "
          f"误报 {result['clean_false_positives']} 行 · 错误码 {result['clean_error_codes']}")
    print(f"  注入错误 {result['injected_error_rows']} 行 + 单位问题 "
          f"{result['injected_warning_rows']} 行")
    print(f"  抓到 {result['caught_error_rows']} 行 → recall {result['recall']:.2%} · "
          f"逐码 {result['per_code']}")
    print(f"  脏数据准确率 {result['dirty_accuracy']:.4%}，"
          f"{'触发' if result['below_threshold'] else '未触发'} 0.998 阈值告警")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
