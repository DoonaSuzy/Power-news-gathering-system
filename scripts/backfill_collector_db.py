"""把中台库的清洗后文章回填到采集库，让中台 API 有数据可查。

背景
----
``newsplatform`` 的 ORM 用 ``CollectorBase`` 映射**采集库** ``powernews``
（articles / sources / provinces / crawl_jobs ...），而本项目实际采集产物落在了
中台库 ``powernews_platform``（见 ``scripts/load_platform_db.py``）。

两边对不上会导致：API 能正常启动，但 ``GET /api/v1/articles`` 查出来是 0 条。

本脚本解决两件事
----------------
1. **补齐来源**：采集库 ``sources`` 只有 39 条（``SOURCES``），
   中台库有 45 条（含 6 条 gov）。缺的 6 条里有 3 条是有文章的，
   不补上这些文章就会因外键缺失而丢失。
2. **转写文章**：把 ``powernews_platform.articles`` 的清洗后正文写进
   ``powernews.articles``，字段名与类型严格对齐 ``sql/schema.sql``，
   ``source_id`` 由 ``adapter_name`` 映射为采集库的整型 id。

幂等性
------
身份 = ``(source_id, url_hash)``（与采集层的唯一键 ``uk_articles_source_hash`` 一致）。
重复执行只更新、不新增，可安全重跑。

用法::

    export PLATFORM_DB_USER=powernews
    export PLATFORM_DB_PASSWORD='你的口令'
    python scripts/backfill_collector_db.py              # 执行
    python scripts/backfill_collector_db.py --dry-run    # 只统计不落库
    python scripts/backfill_collector_db.py --truncate   # 先清空采集库 articles 再灌
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

for _key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_key, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymysql  # noqa: E402

from newsagg.cleaning import content_hash as body_hash  # noqa: E402
from newsagg.sources_seed import ALL_SOURCES  # noqa: E402
from newsagg.urls import normalize_url, url_hash  # noqa: E402

# --------------------------------------------------------------------------
# 连接参数（凭据只走环境变量，源码不含任何真实口令）
# --------------------------------------------------------------------------
COLLECTOR_DB = {
    "host": os.environ.get("COLLECTOR_DB_HOST", os.environ.get("PLATFORM_DB_HOST", "127.0.0.1")),
    "port": int(os.environ.get("COLLECTOR_DB_PORT", os.environ.get("PLATFORM_DB_PORT", "3306"))),
    "user": os.environ.get("COLLECTOR_DB_USER", os.environ.get("PLATFORM_DB_USER", "root")),
    "password": os.environ.get("COLLECTOR_DB_PASSWORD", os.environ.get("PLATFORM_DB_PASSWORD", "")),
    "database": os.environ.get("COLLECTOR_DB_NAME", "powernews"),
    "charset": "utf8mb4",
}

PLATFORM_DB = {
    "host": COLLECTOR_DB["host"],
    "port": COLLECTOR_DB["port"],
    "user": COLLECTOR_DB["user"],
    "password": COLLECTOR_DB["password"],
    "database": os.environ.get("PLATFORM_DB_NAME", "powernews_platform"),
    "charset": "utf8mb4",
}

#: 采集库列宽保护（见 sql/schema.sql）
URL_MAX = 768
TITLE_MAX = 512
SOURCE_NAME_MAX = 128
FLAGS_MAX = 512


def connect(params: dict) -> pymysql.connections.Connection:
    """连接并返回 DictCursor 连接。"""
    conn = pymysql.connect(
        host=params["host"], port=params["port"], user=params["user"],
        password=params["password"], database=params["database"],
        charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    print(f"[db] 已连接 {params['database']} @ {params['host']}:{params['port']}")
    return conn


def clip(value: str | None, limit: int) -> str:
    """按列宽截断（超长宁可截断也不让整批写入失败）。"""
    text = "" if value is None else str(value)
    return text[:limit]


# --------------------------------------------------------------------------
# 1. 补齐来源
# --------------------------------------------------------------------------
def ensure_sources(conn, *, dry_run: bool = False) -> dict[str, int]:
    """确保采集库拥有 ``ALL_SOURCES`` 里的全部适配器，返回 ``adapter_name -> id``。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id, adapter_name FROM sources")
        existing = {r["adapter_name"]: int(r["id"]) for r in cur.fetchall()}

    # sources_seed 的字段名是 ``id``（即适配器名）与 ``interval_hours``，
    # 对应采集库的 ``adapter_name`` 与 ``crawl_interval_hours``。
    missing = [s for s in ALL_SOURCES if s.get("id") not in existing]
    print(f"[sources] 采集库现有 {len(existing)} 条，待补 {len(missing)} 条")

    if missing and not dry_run:
        with conn.cursor() as cur:
            for src in missing:
                adapter = src.get("id") or ""
                base_url = (src.get("base_url") or "").rstrip("/")
                news_path = src.get("news_path") or ""
                cur.execute(
                    "INSERT INTO sources "
                    "(adapter_name, source_id, name, province_code, base_url, news_path, "
                    " list_url, category, priority, crawl_interval_hours, enabled, status, note) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        clip(adapter, 64),
                        clip(adapter, 64),
                        clip(src.get("name"), 128),
                        src.get("province_code"),
                        clip(base_url, 512),
                        clip(news_path, 255),
                        clip(f"{base_url}{news_path}", 512),
                        src.get("category") or "sgcc",
                        int(src.get("priority") or 0),
                        int(src.get("interval_hours") or 1),
                        1 if src.get("enabled", True) else 0,
                        "active",
                        clip(src.get("note"), 512),
                    ),
                )
                print(f"[sources]   + {adapter} ({src.get('name')})")

    if dry_run and missing:
        return existing

    with conn.cursor() as cur:
        cur.execute("SELECT id, adapter_name FROM sources")
        return {r["adapter_name"]: int(r["id"]) for r in cur.fetchall()}


# --------------------------------------------------------------------------
# 2. 转写文章
# --------------------------------------------------------------------------
def parse_dt(value) -> datetime | None:
    """把 platform 库里的时间列转成 ``datetime``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_flags(raw) -> str:
    """``quality_flags`` 从 platform 的 JSON 列转成采集库的字符串列。"""
    if raw is None:
        return "[]"
    if isinstance(raw, (list, dict)):
        return clip(json.dumps(raw, ensure_ascii=False), FLAGS_MAX)
    text = str(raw).strip()
    if not text:
        return "[]"
    try:
        parsed = json.loads(text)
        return clip(json.dumps(parsed, ensure_ascii=False), FLAGS_MAX)
    except (ValueError, TypeError):
        return clip(text, FLAGS_MAX)


def split_paragraphs(body: str) -> list[str]:
    """正文按空行/换行拆段（供 ``content_hash`` 计算，与原采集口径一致）。"""
    if not body:
        return []
    chunks: list[str] = []
    for block in body.split("\n"):
        piece = block.strip()
        if piece:
            chunks.append(piece)
    return chunks


def fetch_platform_rows(pconn) -> list[dict]:
    """取中台库全部文章，并带上来源名与适配器名。"""
    sql = (
        "SELECT a.id, a.source_id AS adapter_name, a.province_code, a.category, "
        "       a.title, a.body_text, a.published_at_utc, a.published_at_cst, "
        "       a.time_precision, a.url, a.quality_flags, s.source_name "
        "FROM articles a "
        "LEFT JOIN sources s ON s.source_id = a.source_id "
        "ORDER BY a.id"
    )
    with pconn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    print(f"[platform] 读到 {len(rows)} 篇")
    return rows


def backfill(conn, pconn, src_map: dict[str, int], *, dry_run: bool = False) -> dict[str, int]:
    """把中台库文章写进采集库（按 ``(source_id, url_hash)`` upsert）。"""
    rows = fetch_platform_rows(pconn)
    stat = {"saved": 0, "updated": 0, "skipped": 0, "no_source": 0, "no_url": 0}

    with conn.cursor() as cur:
        # 预取已有文章：``(source_id, url_hash) -> id``
        cur.execute("SELECT id, source_id, url_hash FROM articles")
        existing = {
            (int(r["source_id"]), r["url_hash"]): int(r["id"])
            for r in cur.fetchall()
        }

    for row in rows:
        url = (row.get("url") or "").strip()
        if not url:
            stat["no_url"] += 1
            continue

        adapter = (row.get("adapter_name") or "").strip()
        source_id = src_map.get(adapter)
        if source_id is None:
            stat["no_source"] += 1
            continue

        uh = url_hash(url)
        body = row.get("body_text") or ""
        chash = body_hash(split_paragraphs(body))
        payload = (
            source_id,
            uh,
            clip(normalize_url(url), URL_MAX),
            clip(row.get("title"), TITLE_MAX),
            body,
            clip(row.get("source_name"), SOURCE_NAME_MAX),
            parse_dt(row.get("published_at_utc")),
            parse_dt(row.get("published_at_cst")),
            clip(row.get("time_precision") or "unknown", 16),
            clip(chash, 40),
            normalize_flags(row.get("quality_flags")),
        )

        key = (source_id, uh)
        if not dry_run:
            with conn.cursor() as cur:
                if key in existing:
                    cur.execute(
                        "UPDATE articles SET url=%s, title=%s, body_text=%s, source_name=%s, "
                        "published_at_utc=%s, published_at_cst=%s, time_precision=%s, "
                        "content_hash=%s, quality_flags=%s, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=%s",
                        (payload[2], payload[3], payload[4], payload[5], payload[6],
                         payload[7], payload[8], payload[9], payload[10], existing[key]),
                    )
                    stat["updated"] += 1
                else:
                    cur.execute(
                        "INSERT INTO articles "
                        "(source_id, url_hash, url, title, body_text, source_name, "
                        " published_at_utc, published_at_cst, time_precision, content_hash, "
                        " quality_flags) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        payload,
                    )
                    existing[key] = int(cur.lastrowid)
                    stat["saved"] += 1
        else:
            stat["saved" if key not in existing else "updated"] += 1

    return stat


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="回填采集库 articles/sources")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--truncate", action="store_true", help="灌之前先清空采集库 articles")
    args = ap.parse_args()

    if not COLLECTOR_DB["user"]:
        print("[x] 未指定数据库账号（COLLECTOR_DB_USER / PLATFORM_DB_USER）")
        return 2

    conn = connect(COLLECTOR_DB)
    pconn = connect(PLATFORM_DB)

    if args.truncate and not args.dry_run:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM articles")
        print("[collector] 已清空 articles")

    src_map = ensure_sources(conn, dry_run=args.dry_run)
    print(f"[sources] 映射表就绪：{len(src_map)} 个适配器")

    stat = backfill(conn, pconn, src_map, dry_run=args.dry_run)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM articles")
        total = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM sources")
        nsrc = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM articles WHERE body_text IS NULL OR body_text=''")
        empty = int(cur.fetchone()["n"])

    print()
    print("=" * 52)
    print(f"  新增 {stat['saved']}  更新 {stat['updated']}  "
          f"缺来源 {stat['no_source']}  缺链接 {stat['no_url']}")
    print(f"  采集库 articles 合计: {total}"
          f"{'  (dry-run，未实际写入)' if args.dry_run else ''}")
    print(f"  采集库 sources  合计: {nsrc}")
    print(f"  空正文: {empty}")
    print("=" * 52)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
