"""批量采集：把时间窗口扩到「当年 7 月 1 日 → 现在」并采集全部启用的来源。

## 为什么单独写脚本

`newsagg crawl-once` 一次只处理**一个**来源。要跑 45 个源、还要收集
「每个源采了多少 / 失败原因」的统计，靠手工敲 45 条命令不现实。

## 关键设计

**① 时间窗口从「当年 7 月 1 日」动态算，不写死天数**

用户要求「当年 7 月起始至当前时间的完整区间」。写死 `--days 104` 会在
跨年后悄悄变质（比如 1 月跑时仍是「104 天前」而非「去年 7 月 1 日」）。
所以这里按日期算：

    若今天 ≥ 7 月 1 日 → 起点 = 今年 7 月 1 日
    否则               → 起点 = 去年 7 月 1 日（跨年场景）

**② 逐源串行，单个失败不中断整体**

某个源挂在网络/反爬上很正常，不能让它把整批拖停。每个源独立 try，
失败只记进报告。

**③ 结果落 JSON**

供统计与后续去重校验复用，避免重复执行。

用法::

    python scripts/batch_crawl.py --dry-run          # 只看会跑哪些源
    python scripts/batch_crawl.py --limit 10         # 先跑 10 个
    python scripts/batch_crawl.py --report out.json  # 结果落盘
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def window_start(today: date | None = None) -> date:
    """算「当年 7 月 1 日」（跨年时回退到去年）。

    Args:
        today: 今天；``None`` 取系统日期。

    Returns:
        窗口起点日期。

    Example:
        >>> window_start(date(2026, 10, 13))
        datetime.date(2026, 7, 1)
        >>> window_start(date(2026, 3, 5))   # 跨年：去年 7 月
        datetime.date(2025, 7, 1)
    """
    today = today or date.today()
    year = today.year if today >= date(today.year, 7, 1) else today.year - 1
    return date(year, 7, 1)


def list_enabled_sources() -> list[tuple[str, str]]:
    """列出启用的来源，返回 ``[(source_id, adapter_name)]``。"""
    from newsagg.adapters import all_adapters, ensure_loaded

    ensure_loaded()
    out: list[tuple[str, str]] = []
    for sid, a in sorted(all_adapters().items()):
        # 只跑电网站群 + 行业媒体（港澳台/企业站结构未适配，跑了也是 0 篇）
        cat = getattr(a, "category", "")
        if cat in ("hmt",):
            continue
        out.append((sid, sid))
    return out


def crawl_one(adapter: str, *, days: int, timeout: int) -> dict:
    """跑单个来源，返回结果字典。

    Note:
        CLI 只提供 ``--days``（回看天数），**没有** ``--since`` ——
        所以调用方要把「当年 7/1」换算成天数传入。
    """
    cmd = [sys.executable, "-m", "newsagg", "crawl-once",
           "--source", adapter, "--days", str(days), "--exec"]

    env = dict(os.environ)
    # 清代理（国网 HTTPS 会被沙箱代理劫持，见 netpolicy 注释）
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"

    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        # 从日志里抠出 job 完成行（含 discovered/saved/status）
        saved = discovered = None
        status = "unknown"
        for line in out.splitlines():
            if "完成:" in line and "'saved'" in line:
                try:
                    d = json.loads(line[line.index("{"):line.rindex("}") + 1])
                    saved = d.get("saved")
                    discovered = d.get("discovered")
                    status = d.get("status", "unknown")
                except (ValueError, KeyError):
                    pass
        return {"adapter": adapter, "status": status,
                "discovered": discovered, "saved": saved,
                "duration_s": round(time.time() - t0, 1),
                "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"adapter": adapter, "status": "timeout", "saved": None,
                "discovered": None, "duration_s": timeout, "returncode": -1}


def main() -> int:
    ap = argparse.ArgumentParser(description="批量采集（当年 7 月至今）")
    ap.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD（默认 = 当年 7/1）")
    ap.add_argument("--limit", type=int, default=0, help="最多跑几个源（0=全部）")
    ap.add_argument("--timeout", type=int, default=900, help="单源超时秒数")
    ap.add_argument("--dry-run", action="store_true", help="只列来源，不采集")
    ap.add_argument("--report", default="data/batch_crawl_report.json", help="结果落盘路径")
    args = ap.parse_args()

    start = (date.fromisoformat(args.since) if args.since else window_start())
    days = (date.today() - start).days + 1
    since = start.isoformat()
    sources = list_enabled_sources()
    if args.limit:
        sources = sources[: args.limit]

    print(f"时间窗口：{since} ~ {date.today().isoformat()}（{days} 天）")
    print(f"来源数：{len(sources)}")
    if args.dry_run:
        for sid, _ in sources:
            print(f"  {sid}")
        return 0

    results: list[dict] = []
    t0 = time.time()
    for i, (sid, adapter) in enumerate(sources, 1):
        print(f"[{i}/{len(sources)}] {sid} ...", flush=True)
        r = crawl_one(adapter, days=days, timeout=args.timeout)
        results.append(r)
        print(f"      -> status={r['status']} saved={r['saved']} "
              f"discovered={r['discovered']} {r['duration_s']}s", flush=True)

    # 汇总
    ok = [r for r in results if r["status"] == "succeeded"]
    total_saved = sum(r["saved"] or 0 for r in results)
    total_disc = sum(r["discovered"] or 0 for r in results)
    summary = {
        "since": since,
        "until": date.today().isoformat(),
        "sources_total": len(results),
        "sources_succeeded": len(ok),
        "total_saved": total_saved,
        "total_discovered": total_disc,
        "elapsed_s": round(time.time() - t0, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }
    out = PROJECT_ROOT / args.report
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"来源 {len(results)} 个：成功 {len(ok)}")
    print(f"新增入库 {total_saved} 篇（候选 {total_disc} 条）")
    print(f"耗时 {summary['elapsed_s']}s")
    print(f"报告：{out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
