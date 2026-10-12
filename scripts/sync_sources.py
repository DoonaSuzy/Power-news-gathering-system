"""把 ``newsagg/sources_seed.py`` 的来源清单**全量同步**到采集库的 ``sources`` 表。

为什么需要它
------------
``newsagg/db.py::seed_sources`` 只遍历 ``SOURCES``（原 39 条），
**不含 ``ADDITIONAL_SOURCES``**（政府能源主管部门 + 本次新增的全国性来源）。
于是新加的源在代码里存在、在库里没有 —— 采集器建批时按库里的源走，
结果是「适配器写了却永远不跑」，且日志一切正常。

本脚本以 ``ALL_SOURCES`` 为准做 upsert：
- 库里没有 → 插入；
- 库里已有 → 只更新**配置列**（名称/URL/类别/优先级/间隔/备注），
  **不动** ``enabled`` / ``status`` / 冷却时间等运行时状态列
  （那是运营在界面上改过的，不该被代码覆盖）。

用法::

    python scripts/sync_sources.py --dry-run    # 只看差异
    python scripts/sync_sources.py              # 实际同步
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiomysql  # noqa: E402

from newsagg.sources_seed import ALL_SOURCES, list_url  # noqa: E402

#: 允许被本脚本覆盖的配置列。**运行时状态列不在其中** —— 运营在界面上
#: 改过的 enabled/status/冷却时间，不该被代码同步覆盖掉。
CONFIG_COLUMNS = (
    "name", "province_code", "base_url", "news_path", "list_url",
    "category", "priority", "crawl_interval_hours", "note",
)


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


async def main() -> int:
    ap = argparse.ArgumentParser(description="同步来源清单到 sources 表")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写库")
    args = ap.parse_args()

    conn = await aiomysql.connect(**_dsn())
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT adapter_name FROM sources")
            existing = {r[0] for r in await cur.fetchall()}

        new = [s for s in ALL_SOURCES if s["id"] not in existing]
        print(f"代码里共 {len(ALL_SOURCES)} 个来源，库内已有 {len(existing)} 个")
        print(f"需要**新增** {len(new)} 个：" if new else "无新增来源")
        for s in new:
            print(f"  + {s['id']:14s} {s['name'][:20]:22s} {s['category']:12s} {list_url(s)[:52]}")

        if args.dry_run:
            print("\ndry-run：未写库")
            return 0
        if not new:
            return 0

        cols = ("adapter_name", "source_id", *CONFIG_COLUMNS, "enabled")
        placeholders = ",".join(["%s"] * len(cols))
        updates = ",".join(f"{c}=VALUES({c})" for c in CONFIG_COLUMNS)
        sql = (
            f"INSERT INTO sources ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        written = 0
        async with conn.cursor() as cur:
            for s in new:
                await cur.execute(sql, (
                    s["id"], s["id"],
                    s["name"], s["province_code"], s["base_url"], s["news_path"],
                    list_url(s), s["category"], s["priority"], s["interval_hours"],
                    s["note"], 1 if s["enabled"] else 0,
                ))
                written += 1
        await conn.commit()
        print(f"\n已写入 {written} 条新来源。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
