"""把 mysqldump 产物按语句粒度重放（绕过 mysql CLI 的超长行解析缺陷）。

背景：26 MB 的 dump 里单个 INSERT 语句就有几十万字符，`mysql` 客户端在
Windows 上按行缓冲时会从中截断，把 `\\"` 当成客户端命令，报
``Unknown command '\\"'`` 并中止——**备份本身是好的，是恢复方式有问题**。

用法::

    python scripts/restore_dump.py backups/power_news_20261027.sql --host 127.0.0.1 --user root
    python scripts/restore_dump.py dump.sql --dry-run        # 只解析不执行
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path


def split_statements(text: str) -> Iterator[str]:
    """按分号切分 SQL，正确跳过字符串、反引号与注释里的分号。"""
    buf: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                buf.append("*/")
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"'):
                # 反斜杠转义：把下一个字符原样吞掉，避免把 \' 当成字符串结束
                if i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                # '' 或 "" 是转义后的同一个引号
                if nxt == quote:
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2] in " \t\r\n"):
            in_line_comment = True
            buf.append(ch)
        elif ch == "#":
            in_line_comment = True
            buf.append(ch)
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append("/*")
            i += 2
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            buf.clear()
            if stmt:
                yield stmt
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按语句重放 mysqldump 产物")
    parser.add_argument("dump", help="dump 文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default=None, help="目标库（dump 内没有 USE 时必填）")
    parser.add_argument("--dry-run", action="store_true", help="只统计语句，不执行")
    args = parser.parse_args(argv)

    text = Path(args.dump).read_text(encoding="utf-8", errors="replace")
    statements = list(split_statements(text))
    print(f"解析到 {len(statements)} 条语句")

    if args.dry_run:
        for stmt in statements[:10]:
            print("  ", stmt[:80].replace("\n", " "))
        return 0

    import pymysql

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.database,
        charset="utf8mb4", autocommit=True,
        max_allowed_packet=256 * 1024 * 1024,
    )
    done = 0
    try:
        with conn.cursor() as cur:
            # mysqldump 默认会关掉外键检查；按语句重放时这行可能被跳过，
            # 导致 article_tasks 建表时找不到 crawl_jobs。这里显式关掉再恢复。
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            # LOCK / UNLOCK 也照原样执行：dump 内部的锁与 ALTER 是配套的，
            # 跳过锁反而会让后续 INSERT 报 "Table was not locked"。
            for stmt in statements:
                cur.execute(stmt)
                done += 1
                if done % 10 == 0:
                    print(f"  已执行 {done}/{len(statements)}", flush=True)
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        conn.close()
    print(f"完成：执行 {done} 条语句")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
