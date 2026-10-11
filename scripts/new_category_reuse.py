"""量「新增一类时序数据到底复用了多少既有代码」——给简历里那条复用率/开发周期做证据。

口径说明（为什么这样量）
------------------------
简历写「代码复用率提升 60%、新增接口开发周期从 3 天缩短至 2 小时」。这两个词都
可以被随意解释，所以这里钉一个可复跑的算法：

* **新写**：接入 ``lng_price`` 时真正新增的生产代码——只有一处注册表文件里的
  三段声明（枚举成员、类别画像、真实库落点）。行数用 ``git numstat`` 核，不靠回忆。
* **复用**：一次 ``PowerDataClient.query("气价")`` 在运行时**实际执行到**的
  powersdk 源码行数（按文件去重后的行号集合大小）。用 ``sys.settrace`` 采集，
  不是「模块里一共有多少行」——没执行到的代码不算复用，只算体积。
* 复用率 = ``复用 / (复用 + 新写)``。这是「同样一份能力，多少比例来自既有代码」。

trace 只统计 powersdk 自己的帧（帧回调对包外文件返回 ``None``，关掉那一帧的行事件），
否则会把 pandas 内部每一行都跟一遍，测的是解释器开销而不是复用面。

用法::

    python scripts/new_category_reuse.py                 # 离线源，默认查 lng_price
    python scripts/new_category_reuse.py --category 电价
    python scripts/new_category_reuse.py --json out.json  # 机器可读，写进文档用

配好 ``POWERSDK_API_BASE_URL`` 时走中台真实数据，测量口径不变。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

import powersdk
from powersdk import PowerDataClient, categories, resolve_category
from powersdk.client import default_source

POWERSDK_ROOT = Path(powersdk.__file__).parent


def _in_powersdk(filename: str) -> bool:
    return filename.startswith(str(POWERSDK_ROOT) + os.sep)


def trace_query(client: PowerDataClient, **kwargs: object) -> tuple[pd.DataFrame, dict[str, int], float]:
    """执行一次 query，返回（结果, 每个文件执行到的去重行数, 耗时秒）。"""
    executed: dict[str, set[int]] = defaultdict(set)

    def frame_trace(frame, event: str, _arg: object):
        if not _in_powersdk(frame.f_code.co_filename):
            return None  # 包外帧：不收行事件，避免把 pandas 内部全跟一遍
        if event == "call":
            return frame_trace
        if event == "line":
            executed[frame.f_code.co_filename].add(frame.f_lineno)
        return None

    previous = sys.gettrace()
    sys.settrace(frame_trace)
    started = time.perf_counter()
    try:
        frame = client.query(**kwargs)
    finally:
        elapsed = time.perf_counter() - started
        sys.settrace(previous)
    return frame, {Path(f).name: len(lines) for f, lines in executed.items()}, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--category", default="lng_price", help="要量的类别（中文名或规范名）")
    parser.add_argument("--region", default=None, help="地区过滤，留空为全部")
    parser.add_argument("--start", default="2025-01-01", help="离线内存源的窗口是 2025-01；中台传真实日期")
    parser.add_argument("--end", default="2025-01-03")
    parser.add_argument("--granularity", default="daily")
    parser.add_argument("--param", default=None, help="口径（行情类是 grade，气象类是要素列）")
    # 13 = Category 枚举 1 行 + CategorySpec 7 行 + SourceBinding 5 行，全在 timeseries.py 一个文件里，
    # 用 ``git diff --numstat`` 对着这三段声明核过，不是估的。
    parser.add_argument("--new-lines", type=int, default=13, help="新写行数（用 git numstat 核对后填入）")
    parser.add_argument("--json", dest="json_path", default=None, help="把测量结果写到这里")
    args = parser.parse_args(argv)

    category = resolve_category(args.category).value
    client = PowerDataClient(source=default_source())
    frame, per_file, elapsed = trace_query(
        client,
        category=category,
        region=args.region,
        start=args.start,
        end=args.end,
        granularity=args.granularity,
        param=args.param,
        fill=False,  # 补缺与否不影响「复用了哪些既有行」的口径，关掉让测量更干净
    )
    reused = sum(per_file.values())
    new_lines = args.new_lines

    print(f"类别 {category}（{len(categories())} 类之一）  数据源 {type(client.source).__name__}")
    print(f"返回 {len(frame)} 行 / {frame['region'].nunique() if len(frame) else 0} 个地区，"
          f"耗时 {elapsed:.3f}s（含 trace 开销）")
    print(f"这次调用实际执行到的 powersdk 源码行：{reused} 行，分布在 {len(per_file)} 个文件")
    for name, count in sorted(per_file.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} {count:>5}")
    print(f"新写 {new_lines} 行 → 复用率 {reused / (reused + new_lines):.1%}")

    if args.json_path:
        payload = {
            "category": category,
            "source": type(client.source).__name__,
            "rows": int(len(frame)),
            "regions": int(frame["region"].nunique()) if len(frame) else 0,
            "elapsed_seconds_with_trace": round(elapsed, 3),
            "reused_lines": reused,
            "new_lines": new_lines,
            "reuse_ratio": round(reused / (reused + new_lines), 6),
            "per_file": dict(sorted(per_file.items(), key=lambda kv: -kv[1])),
            "accuracy": client.last_accuracy(),
            "alert_codes": [a.code for a in client.alerts],
        }
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
