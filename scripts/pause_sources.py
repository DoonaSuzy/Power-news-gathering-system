#!/usr/bin/env python3
"""暂停 / 恢复来源（把风控严重的站点请出生产）。

为什么需要单独一个脚本
----------------------
`enabled=0` 只是数据库里一个字段，但「停谁、为什么停、怎么恢复」需要留痕。
直接 UPDATE 的话，过两周没人记得这批是为什么停的，也没法一次性恢复。

用法::

    python scripts/pause_sources.py --list                  # 看当前停用情况
    python scripts/pause_sources.py --adapters sgcc_jl,sgcc_md --reason river_security --dry-run
    python scripts/pause_sources.py --adapters sgcc_jl,sgcc_md --reason river_security
    python scripts/pause_sources.py --resume --reason river_security   # 按原因恢复

`note` 记录停用原因，恢复时按原因批量匹配——比记 adapter 列表可靠。
（采集库 `sources` 表用 `note` 存原因、`status` 存状态，没有 pause_reason 列。）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg.config import get_settings  # noqa: E402
from newsagg.db import Database  # noqa: E402

__all__ = ["list_sources", "set_enabled"]


def _connect() -> Database:
    return Database(get_settings())


def list_sources(db: Database) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT adapter_name, name, province_code, category, enabled, status, note "
            "FROM sources ORDER BY enabled, category, adapter_name"
        )
        return list(cur.fetchall())


def set_enabled(
    db: Database,
    *,
    adapters: list[str] | None = None,
    reason: str = "",
    enabled: bool,
    dry_run: bool = False,
) -> list[str]:
    """按 adapter 列表或原因批量改启用状态，返回受影响的 adapter。"""
    with db.connect() as conn, conn.cursor() as cur:
        if adapters:
            marks = ",".join(["%s"] * len(adapters))
            cur.execute(
                f"SELECT adapter_name FROM sources WHERE adapter_name IN ({marks})",
                tuple(adapters),
            )
        else:
            cur.execute(
                "SELECT adapter_name FROM sources WHERE note = %s", (reason,)
            )
        targets = [r["adapter_name"] for r in cur.fetchall()]

        if not targets or dry_run:
            return targets

        marks = ",".join(["%s"] * len(targets))
        cur.execute(
            f"UPDATE sources SET enabled=%s, status=%s, note=%s "
            f"WHERE adapter_name IN ({marks})",
            (
                1 if enabled else 0,
                "active" if enabled else "paused",
                "" if enabled else reason,
                *targets,
            ),
        )
        conn.commit()
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="暂停/恢复采集来源")
    ap.add_argument("--adapters", help="逗号分隔的 adapter 名")
    ap.add_argument("--reason", default="", help="停用原因（恢复时按它匹配）")
    ap.add_argument("--resume", action="store_true", help="恢复（enabled=1）")
    ap.add_argument("--list", action="store_true", help="只列出当前状态")
    ap.add_argument("--dry-run", action="store_true", help="只打印将影响哪些源")
    args = ap.parse_args(argv)

    db = _connect()

    if args.list:
        rows = list_sources(db)
        paused = [r for r in rows if not r["enabled"]]
        print(f"共 {len(rows)} 个来源，其中停用 {len(paused)} 个")
        for r in paused:
            print(
                f"  停用 {r['adapter_name']:<14} {r['note'] or '(无原因)'} "
                f"[{r['status'] or '-'}]"
            )
        return 0

    adapters = [a.strip() for a in (args.adapters or "").split(",") if a.strip()] or None
    if not adapters and not (args.resume and args.reason):
        print("需要 --adapters，或 --resume --reason <原因>")
        return 1

    targets = set_enabled(
        db,
        adapters=adapters,
        reason=args.reason,
        enabled=args.resume,
        dry_run=args.dry_run,
    )
    action = "恢复" if args.resume else "停用"
    if args.dry_run:
        print(f"[dry-run] 将{action} {len(targets)} 个：{', '.join(targets) or '无'}")
    else:
        print(f"已{action} {len(targets)} 个：{', '.join(targets) or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
