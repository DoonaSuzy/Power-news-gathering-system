"""把归档数据写入数据中台库 powernews_platform。

流程
----
1. 执行 ``sql/platform_schema.sql`` 建表建视图（幂等）；
2. 载入维度：provinces（34）/ sources（45，含攻克方式与卡点）；
3. 载入事实：articles（按 url_hash 全局去重，INSERT ... ON DUPLICATE KEY UPDATE）；
4. 记录 etl_runs 批次留痕；
5. 校验并打印质量视图。

数据来源（真实可追溯）
    data/power_news/<省份>/<站点>/articles.json   每源完整归档（含 crawler 标记）
    data/power_news/_probe_report.json            实时探活（用于判定卡点）
    data/power_news/_crawl_report.json            抓取报告

用法：
    python scripts/load_platform_db.py
    python scripts/load_platform_db.py --truncate   # 先清空事实表再灌
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

for _key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_key, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymysql  # noqa: E402

from newsagg.provinces import PROVINCES  # noqa: E402
from newsagg.sources_seed import ALL_SOURCES, CATEGORY_LABELS  # noqa: E402
from newsagg.urls import normalize_url, url_hash  # noqa: E402

DATA_ROOT = PROJECT_ROOT / "data" / "power_news"
SCHEMA_SQL = PROJECT_ROOT / "sql" / "platform_schema.sql"

DB = {
    "host": os.environ.get("PLATFORM_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PLATFORM_DB_PORT", "3306")),
    "user": os.environ.get("PLATFORM_DB_USER", "root"),
    "password": os.environ.get("PLATFORM_DB_PASSWORD", ""),
    "database": os.environ.get("PLATFORM_DB_NAME", "powernews_platform"),
    "charset": "utf8mb4",
}

#: 探活结论 → 卡点类型
BLOCKER_MAP = {
    "瑞数": "ruishu",
    "202": "http202",
    "SPA": "spa",
    "403": "waf403",
    "连接失败": "unreachable",
    "404": "path404",
}

#: 日期提取（兼容 2026-10-10 / 2026年10月10日 / 2026-10-10T08:41:06 / 2026-10-10 19:13:33）
DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


# ---------------------------------------------------------------------------
def connect():
    """连接中台库。

    Security:
        凭据**只从环境变量读取**，源码中不内置任何真实口令。默认按
        ``PLATFORM_DB_USER`` / ``PLATFORM_DB_PASSWORD``；若需备用账号，用
        ``PLATFORM_DB_FALLBACK_USER`` / ``PLATFORM_DB_FALLBACK_PASSWORD`` 指定
        （同样只走环境变量）。

    示例::

        export PLATFORM_DB_USER=powernews
        export PLATFORM_DB_PASSWORD='你的口令'
        python scripts/load_platform_db.py
    """
    candidates = [(DB["user"], DB["password"])]
    fallback_user = os.environ.get("PLATFORM_DB_FALLBACK_USER")
    if fallback_user:
        candidates.append((fallback_user, os.environ.get("PLATFORM_DB_FALLBACK_PASSWORD", "")))

    for user, password in candidates:
        try:
            conn = pymysql.connect(
                host=DB["host"], port=DB["port"], user=user, password=password,
                database=DB["database"], charset="utf8mb4", autocommit=True,
            )
            print(f"[db] 已连接 {DB['database']} @ {DB['host']} (user={user})")
            return conn
        except Exception as exc:  # noqa: BLE001
            print(f"[db] {user} 连接失败: {type(exc).__name__}: {str(exc)[:70]}")
    raise SystemExit("无法连接 powernews_platform")


def ensure_schema(conn) -> None:
    """执行建表脚本。

    Note:
        必须**先剥离 ``--`` 注释行再按 ``;`` 拆分**——否则每个语句块都以
        注释行开头，会被整体误过滤，导致建表静默失败。
    """
    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    with conn.cursor() as cur:
        for st in stmts:
            cur.execute(st)
    print(f"[schema] 已执行 {len(stmts)} 条 DDL/视图语句")


# ---------------------------------------------------------------------------
def parse_datetime(text: str) -> tuple[str | None, str | None, str]:
    """解析发布时间文本 → (date, cst_datetime, precision)。"""
    if not text:
        return None, None, "unknown"
    m = DATE_RE.search(text)
    if not m:
        return None, None, "unknown"
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime(y, mo, d)
    except ValueError:
        return None, None, "unknown"
    tm = TIME_RE.search(text)
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
        ss = int(tm.group(3) or 0)
        if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
            dt = dt.replace(hour=hh, minute=mm, second=ss)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S"), "second"
    return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d 00:00:00"), "day"


# ---------------------------------------------------------------------------
def scan_archives() -> tuple[list[dict], dict]:
    """扫描全部来源归档，返回 (文章列表, 按来源的统计)。"""
    articles: list[dict] = []
    by_source: dict[str, dict] = {}

    for p in sorted(DATA_ROOT.glob("*/*/articles.json")):
        prov_dir = p.parent.parent.name
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {p}: {exc}")
            continue
        sid = payload.get("source_id", "")
        crawler = payload.get("crawler", "http")  # cdp-browser / http
        method = "cdp-browser" if crawler == "cdp-browser" else "http"
        arts = payload.get("articles", [])
        by_source[sid] = {
            "article_count": len(arts),
            "crawl_method": method,
            "list_url": payload.get("list_url", ""),
            "province": payload.get("province") or prov_dir,
        }
        for a in arts:
            title = (a.get("title") or "").strip()
            url = (a.get("url") or "").strip()
            if not url:
                continue
            body = (a.get("body") or "").strip()
            date, cst, prec = parse_datetime(a.get("published") or "")
            articles.append({
                "source_id": sid,
                "province": payload.get("province") or prov_dir,
                "title": title,
                "url": url,
                "url_hash": url_hash(url),
                "body": body,
                "body_length": len(body),
                "title_length": len(title),
                "date": date,
                "cst": cst,
                "precision": prec,
                "method": method,
                "crawled_at": payload.get("crawled_at", ""),
            })
    return articles, by_source


def load_blockers() -> dict[str, str]:
    """从探活报告推导每个来源的卡点类型。"""
    out: dict[str, str] = {}
    probe_path = DATA_ROOT / "_probe_report.json"
    if not probe_path.exists():
        return out
    data = json.loads(probe_path.read_text(encoding="utf-8"))
    for item in data.get("results", []):
        sid = item.get("id")
        if not sid:
            continue
        # 取 HTTP 探活结论（国网站群 HTTPS 普遍不可达，HTTP 才有区分度）
        kinds = [p.get("kind", "") for p in item.get("probes", []) if p.get("scheme") == "http"]
        kind = kinds[0] if kinds else ""
        blocker = "none"
        for key, val in BLOCKER_MAP.items():
            if key in kind:
                blocker = val
                break
        out[sid] = blocker
    return out


# ---------------------------------------------------------------------------
def load_provinces(conn) -> int:
    rows = []
    for i, p in enumerate(PROVINCES, 1):
        rows.append((p.code, p.name, p.short, p.region, 1 if p.is_special else 0, i))
    sql = ("INSERT INTO provinces (province_code, province_name, short_name, region, is_special, sort_order) "
           "VALUES (%s,%s,%s,%s,%s,%s) "
           "ON DUPLICATE KEY UPDATE province_name=VALUES(province_name), short_name=VALUES(short_name), "
           "region=VALUES(region), is_special=VALUES(is_special), sort_order=VALUES(sort_order)")
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_sources(conn, by_source: dict, blockers: dict) -> int:
    prov_by_name = {p.name: p.code for p in PROVINCES}
    rows = []
    for s in ALL_SOURCES:
        sid = s["id"]
        arch = by_source.get(sid, {})
        cnt = arch.get("article_count", 0)
        code = s.get("province_code")
        # 行业源无省份代码，用归档里的省份名兜底（一般为「全国_行业」，非省份则置空）
        if not code and arch.get("province") in prov_by_name:
            code = prov_by_name[arch["province"]]
        status = "ok" if cnt > 0 else "blocked"
        if cnt > 0:
            blocker = "none"
        else:
            # 探活未识别出具体卡点，但确实零产出 → 归为「可达但解析失败」
            probed = blockers.get(sid, "")
            blocker = probed if probed and probed != "none" else "parse"
        rows.append((
            sid, s["name"], code, s["category"], CATEGORY_LABELS.get(s["category"], ""),
            s.get("base_url", ""), arch.get("list_url", ""),
            arch.get("crawl_method", "http"), status, blocker, cnt,
            s.get("note", "")[:512],
        ))
    sql = ("INSERT INTO sources (source_id, source_name, province_code, category, category_label, "
           "base_url, list_url, crawl_method, status, blocker, article_count, note) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON DUPLICATE KEY UPDATE source_name=VALUES(source_name), province_code=VALUES(province_code), "
           "category=VALUES(category), category_label=VALUES(category_label), base_url=VALUES(base_url), "
           "list_url=VALUES(list_url), crawl_method=VALUES(crawl_method), status=VALUES(status), "
           "blocker=VALUES(blocker), article_count=VALUES(article_count), note=VALUES(note)")
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def load_articles(conn, articles: list[dict]) -> tuple[int, int, int]:
    """写入文章（去重 + upsert）。返回 (读到, 新增, 更新)。"""
    prov_by_name = {p.name: p.code for p in PROVINCES}
    src_by_id = {s["id"]: s for s in ALL_SOURCES}
    src_meta = {s["id"]: (s["name"], s["category"], CATEGORY_LABELS.get(s["category"], ""))
                for s in ALL_SOURCES}

    # 按 url_hash 去重（同一篇被多家转载只留首次发现）
    uniq: dict[str, dict] = {}
    for a in articles:
        uniq.setdefault(a["url_hash"], a)

    rows = []
    for h, a in uniq.items():
        sid = a["source_id"]
        code = src_by_id.get(sid, {}).get("province_code") or prov_by_name.get(a["province"])
        _, cat, cat_label = src_meta.get(sid, ("", "", ""))
        flags = []
        if a["body_length"] == 0:
            flags.append("empty_body")
        if not code:
            flags.append("no_province_code")
        host = normalize_url(a["url"]).split("/")[2] if "//" in normalize_url(a["url"]) else ""
        cst = a["cst"]
        utc = None
        if cst:
            utc = (datetime.strptime(cst, "%Y-%m-%d %H:%M:%S") - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((
            h, sid, code, a["province"], cat, a["title"], a["body"],
            a["body_length"], a["title_length"], a["date"], cst, utc, a["precision"],
            a["date"][:7] if a["date"] else "", a["url"], host, a["method"],
            json.dumps(flags, ensure_ascii=False), a["crawled_at"] or None,
        ))

    sql = ("INSERT INTO articles (url_hash, source_id, province_code, province_name, category, title, "
           "body_text, body_length, title_length, published_date, published_at_cst, published_at_utc, "
           "time_precision, published_ym, url, source_host, crawl_method, quality_flags, crawled_at) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON DUPLICATE KEY UPDATE title=VALUES(title), body_text=VALUES(body_text), "
           "body_length=VALUES(body_length), title_length=VALUES(title_length), "
           "published_date=VALUES(published_date), published_at_cst=VALUES(published_at_cst), "
           "published_at_utc=VALUES(published_at_utc), time_precision=VALUES(time_precision), "
           "published_ym=VALUES(published_ym), province_code=VALUES(province_code), "
           "province_name=VALUES(province_name), category=VALUES(category), "
           "source_host=VALUES(source_host), crawl_method=VALUES(crawl_method), "
           "quality_flags=VALUES(quality_flags), crawled_at=VALUES(crawled_at)")

    inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 200):
            chunk = rows[i:i + 200]
            cur.executemany(sql, chunk)
            inserted += cur.rowcount
    skipped = len(articles) - len(uniq)
    return len(articles), inserted, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="写入 powernews_platform")
    ap.add_argument("--truncate", action="store_true", help="先清空事实表")
    args = ap.parse_args()

    conn = connect()
    ensure_schema(conn)

    if args.truncate:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM articles")
        print("[truncate] articles 已清空")

    batch_key = datetime.now().strftime("%Y%m%d-%H%M%S")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO etl_runs (batch_key, source_file, status, started_at) "
            "VALUES (%s,%s,'running',%s)",
            (batch_key, "data/power_news", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    print("\n[1/3] 扫描归档 ...")
    articles, by_source = scan_archives()
    print(f"      读到 {len(articles)} 篇，覆盖 {len(by_source)} 个来源")

    print("[2/3] 载入维度 ...")
    n_prov = load_provinces(conn)
    blockers = load_blockers()
    n_src = load_sources(conn, by_source, blockers)
    print(f"      provinces={n_prov}  sources={n_src}")

    print("[3/3] 载入文章事实 ...")
    read, inserted, skipped = load_articles(conn, articles)
    print(f"      读到 {read}，写入/更新 {inserted}，去重丢弃 {skipped}")

    # 回填来源的首末发布日期
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sources s
                JOIN (SELECT source_id,
                             MIN(published_date) AS f, MAX(published_date) AS l
                      FROM articles GROUP BY source_id) a ON a.source_id = s.source_id
            SET s.first_published = a.f, s.last_published = a.l
        """)
        cur.execute(
            "UPDATE etl_runs SET status='succeeded', rows_read=%s, rows_inserted=%s, "
            "rows_skipped=%s, finished_at=%s WHERE batch_key=%s",
            (read, inserted, skipped, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_key),
        )

    # ---------------- 校验 ----------------
    print("\n" + "=" * 64)
    print("写入完成 · 校验")
    print("=" * 64)
    with conn.cursor() as cur:
        for label, sql in [
            ("provinces", "SELECT COUNT(*) FROM provinces"),
            ("sources", "SELECT COUNT(*) FROM sources"),
            ("articles", "SELECT COUNT(*) FROM articles"),
            ("v_data_quality", "SELECT * FROM v_data_quality"),
        ]:
            cur.execute(sql)
            if label == "v_data_quality":
                cols = [d[0] for d in cur.description]
                vals = cur.fetchone()
                print(f"\n[{label}]")
                for c, v in zip(cols, vals, strict=False):
                    print(f"    {c:22} {v}")
            else:
                print(f"[{label}] {cur.fetchone()[0]} 行")

        print("\n[按省份产出 Top10]")
        cur.execute("SELECT province_name, article_count, province_code FROM v_province_coverage "
                    "WHERE article_count > 0 ORDER BY article_count DESC LIMIT 10")
        print(f"    {'省份':<14}{'文章':>6}  区划码")
        for name, cnt, code in cur.fetchall():
            print(f"    {name:<14}{cnt:>6}  {code or '—'}")

        print("\n[按类别]")
        cur.execute("SELECT category, article_count, source_count FROM v_category_stats ORDER BY article_count DESC")
        for cat, cnt, sc in cur.fetchall():
            print(f"    {cat:<14}{cnt:>6} 篇  / {sc} 源")

        print("\n[按月趋势]")
        cur.execute("SELECT published_ym, article_count FROM v_monthly_trend")
        for ym, cnt in cur.fetchall():
            print(f"    {ym}  {cnt:>5}")

    conn.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
