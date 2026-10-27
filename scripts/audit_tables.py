#!/usr/bin/env python3
"""盘点各库的表：DDL 解析 + 跨库同名检测。

解决什么问题
------------
本项目有 4 个库、56 张表，其中 `articles` / `sources` / `provinces` 在多个库里
各有一份且**列结构不同**。靠肉眼比对 DDL 不现实，这个脚本把「哪个库有哪些表、
哪些表名跨库重复」一次性打出来。

用法::

    python scripts/audit_tables.py
    python scripts/audit_tables.py --with-province "../News Aggregator"
    python scripts/audit_tables.py --json > tables.json

只读 SQL 文件，**不连数据库**——表是否存在、有多少行，要另外查
（见 ``docs/数据域合并与channel说明.md`` 与 ``information_schema`` 查询）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 电力项目内的三份 DDL → 逻辑库名
LOCAL_DDL: dict[str, Path] = {
    "powernews": PROJECT_ROOT / "sql" / "schema.sql",
    "powernews_platform": PROJECT_ROOT / "sql" / "platform_schema.sql",
    "powernews(wx)": PROJECT_ROOT / "sql" / "weather_schema.sql",
}

#: 省级项目的两份 DDL（作为可选对照）
PROVINCE_DDL: dict[str, str] = {
    "newsagg": "sql/schema.sql",
    # 键同时充当报告里的「库名」列，改名会与上面 ``newsagg`` 撞键（platform_schema
    # 会静默顶掉 schema.sql，审计清单少一半），所以带后缀区分，与 LOCAL_DDL 的
    # ``powernews(wx)`` 保持同一种写法。
    "newsagg(platform)": "sql/platform_schema.sql",
}

CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\(", re.I)
VIEW_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+`?(\w+)`?", re.I)
#: 表体里不是列定义的行（约束、索引、注释续行）
NON_COLUMN_RE = re.compile(
    r"(PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN|FULLTEXT|COMMENT|CHECK)\b", re.I
)
#: 多行外键定义被拆行后残留的尾巴
TAIL_RE = re.compile(r"^(ON|REFERENCES|MATCH|CASCADE|SET NULL|RESTRICT)\b", re.I)


def _table_body(text: str, open_paren: int) -> tuple[str, int]:
    """从 ``(`` 的位置出发，返回配平的括号体与结束位置。"""
    depth = 0
    for i in range(open_paren, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i], i
    return text[open_paren + 1 :], len(text)


def parse_ddl(text: str) -> dict[str, dict]:
    """解析一份 DDL，返回 ``{表名: {columns, views}}``。"""
    tables: dict[str, dict] = {}
    for match in CREATE_RE.finditer(text):
        body, _end = _table_body(text, match.end() - 1)
        columns: list[str] = []
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or NON_COLUMN_RE.match(stripped) or TAIL_RE.match(stripped):
                continue
            col = re.match(r"`?(\w+)`?\s+", stripped)
            if col:
                columns.append(col.group(1))
        tables[match.group(1)] = {"columns": columns, "n_cols": len(columns)}
    for match in VIEW_RE.finditer(text):
        name = match.group(1)
        tables.setdefault(name, {"columns": [], "n_cols": 0, "is_view": True})
        tables[name]["is_view"] = True
    return tables


def collect_schemas(with_province: str | None) -> dict[str, dict]:
    """读取全部 DDL，返回 ``{库名: {表名: 元数据}}``。"""
    schemas: dict[str, dict] = {}
    for db, path in LOCAL_DDL.items():
        if path.exists():
            schemas[db] = parse_ddl(path.read_text(encoding="utf-8"))
    if with_province:
        base = Path(with_province)
        if not base.is_absolute():
            base = (PROJECT_ROOT / base).resolve()
        for db, rel in PROVINCE_DDL.items():
            path = base / rel
            if path.exists():
                schemas[db] = parse_ddl(path.read_text(encoding="utf-8"))
    return schemas


def render(schemas: dict[str, dict]) -> str:
    """生成人类可读报告。"""
    lines: list[str] = []
    lines.append("=== 每个库的表数 ===")
    for db, tables in schemas.items():
        real = [t for t, v in tables.items() if not v.get("is_view")]
        views = [t for t, v in tables.items() if v.get("is_view")]
        lines.append(f"{db:24s} 表 {len(real):3d}  视图 {len(views):2d}")

    lines.append("")
    lines.append("=== 跨库同名表（列数不同 = 不能互替）===")
    seen: dict[str, dict[str, int]] = {}
    for db, tables in schemas.items():
        for name, meta in tables.items():
            if meta.get("is_view"):
                continue
            seen.setdefault(name, {})[db] = meta["n_cols"]
    for name, dbs in sorted(seen.items()):
        if len(dbs) > 1:
            detail = " / ".join(f"{db}:{n}列" for db, n in dbs.items())
            lines.append(f"  {name:22s} {detail}")

    lines.append("")
    lines.append("=== 仅单库存在的表 ===")
    for name, dbs in sorted(seen.items()):
        if len(dbs) == 1:
            db, n = next(iter(dbs.items()))
            lines.append(f"  {name:22s} {db}（{n} 列）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="盘点各库表结构，检测跨库同名表")
    parser.add_argument(
        "--with-province",
        metavar="PATH",
        help="同时解析省级项目（News Aggregator）的 DDL，路径为其仓库根目录",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本报告")
    args = parser.parse_args(argv)

    schemas = collect_schemas(args.with_province)
    if not schemas:
        print("未找到任何 DDL 文件", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(schemas, ensure_ascii=False, indent=2))
    else:
        print(render(schemas))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
