"""采集报告 + 结构化数据集导出。

对应两件事：

1. **标准化清洗与结构化输出**（需求 6）
   把 ``articles`` 表里选定来源的文章导出为统一字段的 CSV + JSON：
   ``title / published_at / source / source_id / url / body``
   并做清洗（去 HTML 标签、压缩空白、去广告尾巴、统一时间格式）。

2. **采集报告**（需求 7）
   逐站点给出：任务状态、发现条数、入库条数、命中率、最新文章日期、
   空正文数，以及异常说明（失败原因 / robots 拦截 / 列表为空）。

用法::

    python scripts/crawl_report.py                              # 全部来源
    python scripts/crawl_report.py --source chinapower --source china5e
    python scripts/crawl_report.py --days 30 --out-dir data/reports
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiomysql  # noqa: E402

from newsagg.cleaning import clean_text  # noqa: E402

#: 正文尾部的常见噪音行（清洗时剔除）
_TAIL_NOISE = re.compile(
    r"(责任编辑|编辑：|来源：\s*$|扫一扫|分享到|上一篇|下一篇|"
    r"点击查看更多|相关阅读|本文首发|【纠错】|【责任编辑)",
)

FIELDS = ("title", "published_at", "source", "source_id", "url", "body")


def _dsn() -> dict:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    return {
        "host": os.getenv("POWERNEWS_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("POWERNEWS_MYSQL_PORT", "3306")),
        "user": os.getenv("POWERNEWS_MYSQL_USER", "powernews"),
        "password": os.getenv("POWERNEWS_MYSQL_PASSWORD", ""),
        "db": os.getenv("POWERNEWS_MYSQL_DATABASE", "powernews"),
        "charset": "utf8mb4",
    }


def clean_body(raw: str | None) -> str:
    """正文标准化：去标签 → 逐段清洗 → 丢弃噪音行 → 合并为空行分隔。"""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = []
    for line in text.split("\n"):
        item = clean_text(line)
        if not item or _TAIL_NOISE.search(item):
            continue
        lines.append(item)
    return "\n".join(lines).strip()


async def collect(conn, sources: list[str], days: int | None) -> tuple[list[dict], list[dict]]:
    """一次查询取回：站点统计 + 明细文章。"""
    where = "s.enabled = 1"
    params: list = []
    if sources:
        where += f" AND s.adapter_name IN ({','.join(['%s'] * len(sources))})"
        params += sources
    if days:
        where += " AND a.published_at_cst >= %s"
        params.append(datetime.now() - timedelta(days=days))

    async with conn.cursor() as cur:
        # ---- 站点统计 ----
        await cur.execute(
            f"""
            SELECT s.adapter_name, s.name, s.category, s.list_url, s.enabled,
                   COUNT(a.id)                                        AS saved,
                   SUM(CASE WHEN a.body_text IS NULL
                             OR TRIM(a.body_text) = '' THEN 1 ELSE 0 END) AS empty_body,
                   MAX(a.published_at_cst)                           AS latest,
                   MIN(a.published_at_cst)                           AS earliest,
                   COUNT(DISTINCT a.quality_flags)                    AS flag_kinds
            FROM sources s
            LEFT JOIN articles a ON a.source_id = s.id
            WHERE {where}
            GROUP BY s.adapter_name, s.name, s.category, s.list_url, s.enabled
            ORDER BY s.category, s.adapter_name
            """,
            tuple(params),
        )
        sites = [dict(zip([d[0] for d in cur.description], row, strict=False)) for row in await cur.fetchall()]

        # ---- 任务状态 ----
        # ⚠️ 聚合列**必须带别名**：``dict(zip(cols, row))`` 只认 MySQL 返回的列名，
        # 匿名聚合的列名在 ``cursor.description`` 里取不到，下游按 key 取会 KeyError。
        await cur.execute(
            """
            SELECT src.adapter_name, j.status, COUNT(*)              AS jobs,
                   SUM(j.discovered_count)                         AS discovered,
                   SUM(j.saved_count)                              AS saved,
                   MAX(j.stop_reason)                              AS stop_reason,
                   MAX(j.finished_at_utc)                          AS finished_at
            FROM crawl_jobs j
            JOIN sources src ON src.id = j.source_id
            WHERE j.created_at >= %s
            GROUP BY src.adapter_name, j.status
            """,
            (datetime.now() - timedelta(days=max(days or 30, 1)),),
        )
        jobs = [dict(zip([d[0] for d in cur.description], row, strict=False)) for row in await cur.fetchall()]

        # ---- 明细 ----
        await cur.execute(
            f"""
            SELECT a.id, a.title, a.published_at_cst, a.url, a.body_text,
                   s.adapter_name, s.name, a.quality_flags
            FROM articles a JOIN sources s ON s.id = a.source_id
            WHERE {where}
            ORDER BY a.published_at_cst DESC, a.id DESC
            """,
            tuple(params),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row, strict=False)) for row in await cur.fetchall()]
    return sites, jobs, rows


async def main() -> int:
    ap = argparse.ArgumentParser(description="采集报告 + 结构化数据集导出")
    ap.add_argument("--source", action="append", metavar="ID", help="只看指定来源（可重复）")
    ap.add_argument("--days", type=int, default=30, help="时间窗口（天），默认 30")
    ap.add_argument("--out-dir", default="data/reports", help="输出目录")
    args = ap.parse_args()

    conn = await aiomysql.connect(**_dsn())
    try:
        sites, jobs, rows = await collect(conn, args.source or [], args.days)
    finally:
        conn.close()

    # ---------- 结构化数据集（需求 6）----------
    out = Path(PROJECT_ROOT) / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = out / f"news-{stamp}.csv"
    json_path = out / f"news-{stamp}.json"

    records: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        body = clean_body(r["body_text"])
        title = clean_text(r["title"] or "")
        if not title or not body or r["url"] in seen:
            continue
        seen.add(r["url"])
        records.append({
            "title": title,
            "published_at": r["published_at_cst"].strftime("%Y-%m-%d %H:%M:%S")
            if r["published_at_cst"] else "",
            "source": r["name"],
            "source_id": r["adapter_name"],
            "url": r["url"],
            "body": body,
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FIELDS))
        writer.writeheader()
        writer.writerows(records)
    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"结构化数据集：{len(records)} 条")
    print(f"  CSV  → {csv_path.relative_to(PROJECT_ROOT)}")
    print(f"  JSON → {json_path.relative_to(PROJECT_ROOT)}")

    # ---------- 采集报告（需求 7）----------
    job_by_site: dict[str, list[dict]] = {}
    for j in jobs:
        job_by_site.setdefault(j["adapter_name"], []).append(j)

    lines: list[str] = []
    lines.append("# 电力新闻采集报告\n")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　窗口：近 {args.days} 天\n")
    total_saved = sum(int(s["saved"] or 0) for s in sites)
    total_empty = sum(int(s["empty_body"] or 0) for s in sites)
    lines.append("## 总览\n")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 覆盖来源数 | {len(sites)} |")
    lines.append(f"| 入库文章数 | {total_saved} |")
    lines.append(f"| 空正文数 | {total_empty} |")
    lines.append(f"| 空正文率 | {(total_empty / total_saved * 100 if total_saved else 0):.2f}% |")
    lines.append(f"| 结构化导出 | {len(records)} 条 |")
    lines.append("")

    lines.append("## 逐站点状态\n")
    lines.append("| 来源 | 站点 | 类别 | 入库 | 空正文 | 最新文章 | 任务状态 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sites:
        sid = s["adapter_name"]
        js = job_by_site.get(sid, [])
        statuses = "、".join(f"{j['status']}×{j['jobs']}" for j in js) if js else "无任务"
        if not js:
            note = "窗口内未建批"
        elif any(j["status"] in ("failed", "retry_wait") for j in js):
            reasons = {j["stop_reason"] for j in js if j["stop_reason"]}
            note = "；".join(reasons) or "见 crawl_jobs"
        elif any(j["status"] == "partial" for j in js):
            note = "部分成功"
        else:
            note = "正常"
        if int(s["empty_body"] or 0) and not int(s["saved"] or 0):
            note += "｜全部为空"
        latest = s["latest"].strftime("%Y-%m-%d") if s["latest"] else "—"
        lines.append(
            f"| `{sid}` | {s['name']} | {s['category']} | {s['saved'] or 0} | "
            f"{s['empty_body'] or 0} | {latest} | {statuses} | {note} |"
        )
    lines.append("")

    anomalies = [s for s in sites
                 if not int(s["saved"] or 0)
                 or any(j["status"] in ("failed", "retry_wait")
                        for j in job_by_site.get(s["adapter_name"], []))]
    lines.append("## 异常站点说明\n")
    if anomalies:
        for s in anomalies:
            sid = s["adapter_name"]
            js = job_by_site.get(sid, [])
            discovered = sum(int(j["discovered"] or 0) for j in js)
            reason = "、".join({j["stop_reason"] for j in js if j["stop_reason"]}) or "未记录"
            lines.append(
                f"- **`{sid}`（{s['name']}）**：入库 0 条，发现 {discovered} 条，停止原因 `{reason}`。"
            )
            lines.append(f"  列表页：{s['list_url']}")
    else:
        lines.append("无异常站点。")
    lines.append("")

    lines.append("## 字段说明（结构化数据集）\n")
    lines.append("| 字段 | 含义 |")
    lines.append("|---|---|")
    lines.append("| `title` | 新闻标题（已清洗，去 HTML 标签与站点后缀） |")
    lines.append("| `published_at` | 发布时间（北京时间，秒级；来源缺时间时为空） |")
    lines.append("| `source` | 来源站点名称 |")
    lines.append("| `source_id` | 适配器标识（程序用） |")
    lines.append("| `url` | 原文链接 |")
    lines.append("| `body` | 正文（段落以换行分隔，已剔除「责任编辑」等噪音行） |")

    report_path = out / f"crawl-report-{stamp}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n采集报告 → {report_path.relative_to(PROJECT_ROOT)}")

    # ---- 控制台摘要 ----
    print("\n" + "=" * 78)
    print(f"{'来源':14s}{'入库':>6s}{'空正文':>7s}{'最新':>13s}  状态")
    print("-" * 78)
    for s in sites:
        js = job_by_site.get(s["adapter_name"], [])
        st = "、".join(sorted({j["status"] for j in js})) or "无任务"
        latest = s["latest"].strftime("%Y-%m-%d") if s["latest"] else "—"
        print(f"{s['adapter_name']:14s}{int(s['saved'] or 0):>6d}"
              f"{int(s['empty_body'] or 0):>7d}{latest:>13s}  {st}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
