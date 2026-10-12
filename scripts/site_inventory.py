"""站点采集清单：逐站点统计文章数，并做口径验算。

为什么单独做一个脚本
--------------------
「各站点采了多少篇」这个问题必须**可复现**：报告里写 1,270，
别人跑一次也该得 1,270。所以把统计口径固化成脚本，而不是每次临时写 SQL。

**口径**：按 ``sources.id = articles.source_id`` 左连接分组计数。
一篇文章只属于一个来源（``articles`` 无多来源关联），因此**不存在跨来源重复计数**；
计数用 ``COUNT(a.id)`` 而非 ``COUNT(*)``，后者会把「零产出来源」也算成 1。

脚本末尾做两项验算：
1. 各来源文章数合计 == ``articles`` 全表总数（查遗漏/重复）；
2. 各类别来源数合计 == ``sources`` 全表总数。

用法::

    python scripts/site_inventory.py                     # 控制台全站清单
    python scripts/site_inventory.py --csv data/x.csv    # 另存 CSV
    python scripts/site_inventory.py --only-output       # 只看有产出的
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.db import Database  # noqa: E402

#: 类别中文名（与 sources_seed.CATEGORY_LABELS 保持一致的含义）
CATEGORY_CN = {
    "sgcc": "国家电网省级公司",
    "csg": "南方电网省级公司",
    "independent": "独立电网",
    "hmt": "港澳台",
    "industry": "行业媒体",
    "gov": "政府能源主管部门",
}


def _fetch(conn) -> tuple[list[dict], list[dict], int, int]:
    """取站点明细、类别汇总、文章总数、来源总数。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.adapter_name, s.name, s.category, s.province_code,
                   s.enabled, s.list_url, COUNT(a.id) AS n_articles
            FROM sources s
            LEFT JOIN articles a ON a.source_id = s.id
            GROUP BY s.id, s.adapter_name, s.name, s.category,
                     s.province_code, s.enabled, s.list_url
            ORDER BY n_articles DESC, s.category, s.adapter_name
            """
        )
        sites = list(cur.fetchall())

        cur.execute(
            """
            SELECT s.category,
                   COUNT(DISTINCT s.id)                                   AS n_sources,
                   COUNT(a.id)                                            AS n_articles,
                   COUNT(DISTINCT CASE WHEN a.id IS NOT NULL
                                       THEN s.id END)                     AS n_ok
            FROM sources s
            LEFT JOIN articles a ON a.source_id = s.id
            GROUP BY s.category
            ORDER BY n_articles DESC
            """
        )
        cats = list(cur.fetchall())

        cur.execute("SELECT COUNT(*) AS n FROM articles")
        total_articles = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM sources")
        total_sources = int(cur.fetchone()["n"])

    return sites, cats, total_articles, total_sources


def main() -> int:
    ap = argparse.ArgumentParser(description="站点采集清单与验算")
    ap.add_argument("--csv", help="另存 CSV 路径")
    ap.add_argument("--only-output", action="store_true", help="只列有产出的来源")
    args = ap.parse_args()

    conn = Database().connect()
    try:
        sites, cats, total_articles, total_sources = _fetch(conn)
    finally:
        conn.close()

    shown = [s for s in sites if s["n_articles"]] if args.only_output else sites

    print("=" * 84)
    print("站点采集清单")
    print("=" * 84)
    print(f"{'来源 id':16s}{'站点名称':26s}{'类别':14s}{'省码':8s}{'文章数':>8s}")
    print("-" * 84)
    for s in shown:
        print(
            f"{s['adapter_name']:16s}{s['name'][:24]:26s}"
            f"{CATEGORY_CN.get(s['category'], s['category']):14s}"
            f"{str(s['province_code'] or '—'):8s}{s['n_articles']:>8d}"
        )

    print("-" * 84)
    print(f"{'合计':16s}{len(shown):<26d}{'':14s}{'':8s}{sum(s['n_articles'] for s in shown):>8d}")

    print("\n按类别汇总")
    print("-" * 84)
    print(f"{'类别':22s}{'来源数':>7s}{'有产出':>7s}{'文章数':>9s}")
    for c in cats:
        print(
            f"{CATEGORY_CN.get(c['category'], c['category']):22s}"
            f"{c['n_sources']:>7d}{c['n_ok']:>7d}{c['n_articles']:>9d}"
        )

    # ---------------- 验算 ----------------
    sum_articles = sum(s["n_articles"] for s in sites)
    sum_sources = sum(c["n_sources"] for c in cats)
    print("\n口径验算")
    print("-" * 84)
    print(f"  各来源文章数合计        = {sum_articles:>8d}")
    print(f"  articles 全表总数       = {total_articles:>8d}")
    ok1 = sum_articles == total_articles
    print(f"  差                      = {sum_articles - total_articles:>8d}   "
          f"{'✅ 闭合（无遗漏、无重复）' if ok1 else '❌ 不闭合，需排查'}")
    print(f"  各类别来源数合计        = {sum_sources:>8d}")
    print(f"  sources 全表总数        = {total_sources:>8d}")
    ok2 = sum_sources == total_sources
    print(f"  差                      = {sum_sources - total_sources:>8d}   "
          f"{'✅ 闭合' if ok2 else '❌ 不闭合'}")

    n_out = sum(1 for s in sites if s["n_articles"])
    print(f"\n  有产出来源 {n_out} / {total_sources}"
          f"（{n_out / total_sources * 100 if total_sources else 0:.0f}%）")

    if args.csv:
        out = pathlib.Path(args.csv)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["来源id", "站点名称", "类别", "类别中文", "省份代码", "启用", "文章数", "列表页"])
            for s in sites:
                w.writerow([
                    s["adapter_name"], s["name"], s["category"],
                    CATEGORY_CN.get(s["category"], s["category"]),
                    s["province_code"] or "", s["enabled"],
                    s["n_articles"], s["list_url"],
                ])
        print(f"\nCSV → {out}")

    return 0 if (ok1 and ok2) else 2


if __name__ == "__main__":
    raise SystemExit(main())
