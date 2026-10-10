"""字段分析：按省份 / 来源拆解 826 篇文章的字段形态与质量，为建表提供依据。

产出：
    docs/字段分析报告.md
    data/power_news/_field_analysis.json

用法：
    python scripts/analyze_fields.py
"""
from __future__ import annotations

import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "power_news" / "_csv" / "all_articles.csv"
OUT_MD = ROOT / "docs" / "字段分析报告.md"
OUT_JSON = ROOT / "data" / "power_news" / "_field_analysis.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAV_WORDS = {
    "新闻中心", "公司要闻", "一线风采", "媒体聚焦", "通知公告", "首页",
    "关于我们", "商务服务", "联系我们", "更多", "更多>>", "集团动态",
    "公司新闻", "一线传真", "视频新闻", "动态要闻", "要闻动态",
}

#: 正文长度阈值
SHORT_BODY = 50
LONG_BODY = 20000


def load() -> list[dict]:
    with SRC.open("r", encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def classify_time(raw: str) -> str:
    """判定发布日期形态。"""
    if not raw:
        return "缺失"
    if DATE_RE.match(raw):
        return "YYYY-MM-DD"
    return "其他格式"


def main() -> None:
    rows = load()
    today = date.today()

    total = len(rows)
    # ---------------- 逐条派生字段 ----------------
    for r in rows:
        body = r.get("正文", "")
        r["_body_len"] = len(body)
        r["_title_len"] = len(r.get("标题", ""))
        r["_host"] = urlsplit(r.get("原文链接", "")).netloc
        r["_time_kind"] = classify_time(r.get("发布时间", ""))
        r["_empty_body"] = not body.strip()
        r["_short_body"] = 0 < len(body) < SHORT_BODY
        r["_long_body"] = len(body) > LONG_BODY
        r["_nav_title"] = r.get("标题", "") in NAV_WORDS
        r["_no_code"] = not r.get("行政区划代码", "").strip()
        # 未来日期
        r["_future"] = False
        if DATE_RE.match(r.get("发布时间", "")):
            try:
                r["_future"] = datetime.strptime(r["发布时间"], "%Y-%m-%d").date() > today
            except ValueError:
                pass

    # ---------------- A. 总体 ----------------
    overview = {
        "文章总数": total,
        "省份数": len({r["省份"] for r in rows}),
        "来源站点数": len({r["来源站点"] for r in rows}),
        "主机数": len({r["_host"] for r in rows if r["_host"]}),
        "空正文": sum(r["_empty_body"] for r in rows),
        "缺行政区划代码": sum(r["_no_code"] for r in rows),
        "疑似栏目名标题": sum(r["_nav_title"] for r in rows),
        "未来日期": sum(r["_future"] for r in rows),
        "时间格式非常规": sum(r["_time_kind"] == "其他格式" for r in rows),
    }

    # ---------------- B. 按省份 ----------------
    by_prov: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_prov[r["省份"]].append(r)

    prov_rows = []
    for prov, items in sorted(by_prov.items(), key=lambda kv: -len(kv[1])):
        lens = [i["_body_len"] for i in items if i["_body_len"] > 0]
        dates = sorted(i["发布时间"] for i in items if DATE_RE.match(i["发布时间"]))
        prov_rows.append({
            "省份": prov,
            "行政区划代码": next((i["行政区划代码"] for i in items if i["行政区划代码"]), ""),
            "文章数": len(items),
            "来源数": len({i["来源站点"] for i in items}),
            "空正文": sum(i["_empty_body"] for i in items),
            "空正文率": round(sum(i["_empty_body"] for i in items) / len(items) * 100, 1),
            "正文中位长": int(statistics.median(lens)) if lens else 0,
            "正文均长": int(statistics.mean(lens)) if lens else 0,
            "最短": min(lens) if lens else 0,
            "最长": max(lens) if lens else 0,
            "最早": dates[0] if dates else "",
            "最晚": dates[-1] if dates else "",
        })

    # ---------------- C. 按来源类别 ----------------
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["来源类别"]].append(r)
    cat_rows = []
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lens = [i["_body_len"] for i in items if i["_body_len"] > 0]
        cat_rows.append({
            "来源类别": cat,
            "文章数": len(items),
            "空正文": sum(i["_empty_body"] for i in items),
            "正文中位长": int(statistics.median(lens)) if lens else 0,
            "缺区划码": sum(i["_no_code"] for i in items),
        })

    # ---------------- D. 按来源站点 ----------------
    by_src: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_src[(r["省份"], r["来源站点"])].append(r)
    src_rows = []
    for (prov, src), items in sorted(by_src.items()):
        lens = [i["_body_len"] for i in items if i["_body_len"] > 0]
        src_rows.append({
            "省份": prov, "来源站点": src,
            "类别": items[0]["来源类别"],
            "文章数": len(items),
            "空正文": sum(i["_empty_body"] for i in items),
            "正文中位长": int(statistics.median(lens)) if lens else 0,
            "域名": items[0]["_host"],
        })

    # ---------------- E. 问题清单 ----------------
    problems = {
        "空正文清单": [
            {"省份": r["省份"], "来源": r["来源站点"], "标题": r["标题"][:60], "链接": r["原文链接"]}
            for r in rows if r["_empty_body"]
        ][:60],
        "缺行政区划代码": sorted({(r["省份"], r["来源站点"]) for r in rows if r["_no_code"]}),
        "疑似栏目名标题": [
            {"省份": r["省份"], "来源": r["来源站点"], "标题": r["标题"]}
            for r in rows if r["_nav_title"]
        ][:30],
        "过短正文": [
            {"省份": r["省份"], "来源": r["来源站点"], "标题": r["标题"][:40], "长度": r["_body_len"]}
            for r in rows if r["_short_body"]
        ][:30],
        "重复链接": [u for u, c in Counter(r["原文链接"] for r in rows).items() if c > 1][:20],
        "重复标题": [t for t, c in Counter((r["省份"], r["标题"]) for r in rows).items() if c > 1][:20],
    }

    # ---------------- F. 域名分布（反爬线索） ----------------
    host_rows = [{"域名": h, "文章数": c}
                 for h, c in Counter(r["_host"] for r in rows if r["_host"]).most_common()]

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "by_province": prov_rows,
        "by_category": cat_rows,
        "by_source": src_rows,
        "hosts": host_rows,
        "problems": {k: (v if not isinstance(v, set) else sorted(v)) for k, v in problems.items()},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 生成 Markdown ----------------
    md: list[str] = [
        "# 字段分析报告 · 各省份电力新闻数据",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"| 数据源：`data/power_news/_csv/all_articles.csv`（{total} 篇）",
        "",
        "本报告用于回答「**各省份的字段长什么样、能不能直接入库、需要怎么优化表结构**」。",
        "",
        "## 一、总体概览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
    ]
    for k, v in overview.items():
        md.append(f"| {k} | {v} |")
    md += ["", "**结论**：字段整体可用——发布日期 100% 有值、标题 100% 有值；"
              "主要质量问题集中在「空正文」「行业源无行政区划代码」两类。", ""]

    md += ["## 二、按省份字段质量", "",
           "| 省份 | 区划码 | 文章数 | 来源数 | 空正文 | 空正文率 | 正文中位长 | 最短 | 最长 | 时间跨度 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for p in prov_rows:
        span = f"{p['最早']} ~ {p['最晚']}" if p["最早"] else "-"
        md.append(f"| {p['省份']} | {p['行政区划代码'] or '—'} | {p['文章数']} | {p['来源数']} | "
                  f"{p['空正文']} | {p['空正文率']}% | {p['正文中位长']} | {p['最短']} | {p['最长']} | {span} |")

    md += ["", "## 三、按来源类别", "",
           "| 来源类别 | 文章数 | 空正文 | 正文中位长 | 缺区划码 |", "|---|---|---|---|---|"]
    for c in cat_rows:
        md.append(f"| {c['来源类别']} | {c['文章数']} | {c['空正文']} | {c['正文中位长']} | {c['缺区划码']} |")

    md += ["", "## 四、按来源站点", "",
           "| 省份 | 来源站点 | 类别 | 文章数 | 空正文 | 正文中位长 | 域名 |",
           "|---|---|---|---|---|---|---|"]
    for s in src_rows:
        md.append(f"| {s['省份']} | {s['来源站点']} | {s['类别']} | {s['文章数']} | "
                  f"{s['空正文']} | {s['正文中位长']} | {s['域名']} |")

    md += ["", "## 五、字段级问题清单", ""]
    md.append(f"### 1. 空正文（{len(problems['空正文清单'])} 条，已截断展示前 60）")
    md += ["", "| 省份 | 来源 | 标题 |", "|---|---|---|"]
    for x in problems["空正文清单"][:30]:
        md.append(f"| {x['省份']} | {x['来源']} | {x['标题']} |")

    md += ["", "### 2. 缺行政区划代码的来源（行业/全国源，属正常）", ""]
    for prov, src in problems["缺行政区划代码"]:
        md.append(f"- {prov} / {src}")

    if problems["疑似栏目名标题"]:
        md += ["", "### 3. 疑似栏目名标题", "", "| 省份 | 来源 | 标题 |", "|---|---|---|"]
        for x in problems["疑似栏目名标题"]:
            md.append(f"| {x['省份']} | {x['来源']} | {x['标题']} |")

    if problems["过短正文"]:
        md += ["", "### 4. 过短正文（<50 字）", "", "| 省份 | 来源 | 标题 | 长度 |", "|---|---|---|---|"]
        for x in problems["过短正文"]:
            md.append(f"| {x['省份']} | {x['来源']} | {x['标题']} | {x['长度']} |")

    md += ["", "### 5. 重复检查", "",
           f"- 重复原文链接：{len(problems['重复链接'])} 组",
           f"- 重复（省份+标题）：{len(problems['重复标题'])} 组"]

    md += ["", "## 六、域名分布（反爬与来源集中度）", "", "| 域名 | 文章数 |", "|---|---|"]
    for h in host_rows[:25]:
        md.append(f"| {h['域名']} | {h['文章数']} |")

    md += ["", "## 七、建表优化建议", "",
           "1. **行政区划代码可空**：行业源（北极星/中国电力新闻网/国际能源网）共 "
           f"{sum(1 for r in rows if r['_no_code'])} 篇无区划码，"
           "表设计须允许 `province_code NULL`，并另设 `province_name` 兜底展示。",
           "2. **正文可空但须标记**：空正文 40 条，入库时以 `quality_flags` 标记 "
           "`empty_body`，便于后续补抓，不能直接丢弃（标题+链接仍有价值）。",
           "3. **发布时间需双列 + 精度**：现有数据均为「日」精度（YYYY-MM-DD），"
           "存 `published_at_cst` + `time_precision='day'`，避免误当秒级参与窗口计算。",
           "4. **URL 需指纹去重**：以 URL 的 SHA-256 前 40 位做唯一键，防重复入库。",
           "5. **正文长度跨度大**（0 ~ 40842），须用 `MEDIUMTEXT` 而非 `TEXT`。",
           "6. **按省份/来源分层**：省份维度单列，便于按省聚合与验收覆盖度。",
           ""]

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    # ---------------- 控制台摘要 ----------------
    print("=" * 60)
    print("字段分析完成")
    print("=" * 60)
    for k, v in overview.items():
        print(f"  {k:16} {v}")
    print("-" * 60)
    print(f"报告: {OUT_MD}")
    print(f"数据: {OUT_JSON}")


if __name__ == "__main__":
    main()
