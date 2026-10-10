"""汇总导出：把 data/power_news/<省份>/<站点>/articles.csv 汇总成总表。

以各分站 articles.csv 为准（该文件用的标题提取路径正确；articles.json
中部分来源的 title 字段是栏目名，不可直接用于去重汇总）。

产出（写到 data/power_news/_csv/）：
  all_articles.csv        全量总表（含正文）
  all_articles_lite.csv   精简总表（去正文，便于快速浏览 / 导入表格）
  province_summary.csv    省份维度汇总统计
  source_summary.csv      来源站点维度汇总统计

用法：
    python scripts/export_csv.py
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data" / "power_news"
OUT_DIR = DATA_ROOT / "_csv"

CSV_FIELDS = [
    "序号",
    "省份",
    "行政区划代码",
    "来源类别",
    "来源站点",
    "标题",
    "发布时间",
    "正文",
    "原文链接",
    "抓取时间",
]
LITE_FIELDS = [f for f in CSV_FIELDS if f != "正文"]

DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def norm_date(raw: str) -> str:
    """把各种日期写法归一到 YYYY-MM-DD，失败则原样返回。"""
    if not raw:
        return ""
    m = DATE_RE.search(raw)
    if not m:
        return raw.strip()
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{y}-{mo:02d}-{d:02d}"


def read_source_csv(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception as exc:  # noqa: BLE001
        print(f"  [跳过] {path}: {exc}")
        return []


def main() -> None:
    rows: list[dict] = []
    for csv_path in sorted(DATA_ROOT.glob("*/*/articles.csv")):
        for item in read_source_csv(csv_path):
            item = {k.strip(): (v or "").strip() for k, v in item.items() if k}
            title = item.get("标题", "")
            url = item.get("原文链接", "")
            if not title or not url:
                continue
            rows.append(item)

    # 去重：以原文链接为准（同一篇文章同链接只会出现一次）
    seen: set[str] = set()
    kept: list[dict] = []
    dropped = 0
    for r in rows:
        key = r.get("原文链接", "")
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(r)

    # 排序：省份 → 来源站点 → 发布时间倒序
    kept.sort(
        key=lambda r: (
            r.get("省份", ""),
            r.get("来源站点", ""),
            r.get("发布时间", ""),
        ),
    )

    full = []
    lite = []
    for i, r in enumerate(kept, 1):
        prov = r.get("省份", "")
        code = r.get("行政区划代码", "")
        cat = r.get("来源类别", "")
        src = r.get("来源站点", "")
        title = r.get("标题", "")
        pub = norm_date(r.get("发布时间", ""))
        body = r.get("正文", "")
        url = r.get("原文链接", "")
        fetched = r.get("抓取时间", "")
        full.append([i, prov, code, cat, src, title, pub, body, url, fetched])
        lite.append([i, prov, code, cat, src, title, pub, url, fetched])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "all_articles.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        w.writerows(full)
    with (OUT_DIR / "all_articles_lite.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(LITE_FIELDS)
        w.writerows(lite)

    # 省份汇总
    by_prov: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_prov[r.get("省份", "")].append(r)
    prov_rows = []
    for prov, items in sorted(by_prov.items()):
        sources = sorted({x.get("来源站点", "") for x in items})
        dates = sorted({norm_date(x.get("发布时间", "")) for x in items if x.get("发布时间")})
        prov_rows.append(
            [
                prov,
                len(items),
                len(sources),
                "、".join(sources),
                dates[0] if dates else "",
                dates[-1] if dates else "",
            ]
        )
    with (OUT_DIR / "province_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["省份", "文章数", "来源站点数", "站点列表", "最早发布时间", "最晚发布时间"])
        w.writerows(prov_rows)

    # 来源站点汇总
    by_src: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in kept:
        by_src[(r.get("省份", ""), r.get("来源站点", ""))].append(r)
    src_rows = []
    for (prov, src), items in sorted(by_src.items()):
        dates = sorted({norm_date(x.get("发布时间", "")) for x in items if x.get("发布时间")})
        src_rows.append(
            [
                prov,
                src,
                items[0].get("来源类别", ""),
                len(items),
                dates[0] if dates else "",
                dates[-1] if dates else "",
            ]
        )
    with (OUT_DIR / "source_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["省份", "来源站点", "来源类别", "文章数", "最早发布时间", "最晚发布时间"])
        w.writerows(src_rows)

    # 同步刷新主目录下的 _all_articles.csv（向后兼容）
    with (DATA_ROOT / "_all_articles.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        w.writerows(full)

    print("=" * 60)
    print("汇总导出完成")
    print("=" * 60)
    print(f"输出目录 : {OUT_DIR}")
    print(f"文章总数 : {len(kept)}  (按链接去重丢弃 {dropped})")
    print(f"省份覆盖 : {len(by_prov)}")
    print(f"站点数量 : {len(by_src)}")
    print("-" * 60)
    print(f"{'省份':<14}{'文章':>6}{'站点':>6}")
    total = 0
    for prov, cnt, snum, *_ in prov_rows:
        print(f"{prov:<14}{cnt:>6}{snum:>6}")
        total += cnt
    print("-" * 60)
    print(f"{'合计':<14}{total:>6}{len(by_src):>6}")
    print("=" * 60)


if __name__ == "__main__":
    main()
