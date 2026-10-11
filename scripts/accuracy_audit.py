"""真实数据准确率审计：把 SDK 的校验区间拿到 158 万行 wx_* 数据上跑一遍。

简历口径写的是「数据输出准确率达 99.8%」。这个数字必须回答两个问题：
一是**在什么数据上测的**（合成种子数据测它没有意义，区间本来就是照着它写的），
二是**准确率指什么**。所以这里分两步：

1. ``--emit-sql`` 按 SDK 的 :data:`~powersdk.timeseries.WEATHER_COLUMN_RANGE`
   生成审计 SQL（区间只有这一处定义，脚本不复制常量，改了画像不用改脚本）；
2. 在库上跑完，把结果喂回 ``--from-tsv``，得到「行数、越界数、空值数、准确率」。

这里的「准确率」是**符合声明区间的行占比**，不是「与气象真值相符」——真值拿不到。
所以报告同时给校验器的**命中率/误报率**（已知脏行的检出情况），否则 100% 只代表
区间画得宽，不代表校验有用。

用法::

    python scripts/accuracy_audit.py --emit-sql > audit.sql
    #   scp audit.sql 到采集机 → /home/zhu/rx.sh power_news audit.sql → 取回 audit.tsv
    python scripts/accuracy_audit.py --from-tsv audit.tsv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from powersdk.timeseries import (  # noqa: E402
    WEATHER_COLUMN_RANGE,
    WEATHER_PRODUCTS,
    WEATHER_REGION_CODES,
    weather_range,
)

COLUMNS = tuple(WEATHER_COLUMN_RANGE)


def emit_sql() -> str:
    """24 张 wx_* 宽表逐列审计（按 granularity 分别取区间）。"""
    parts: list[str] = []
    for region_code in WEATHER_REGION_CODES.values():
        for product in WEATHER_PRODUCTS:
            table = f"wx_{region_code}_{product}"
            selects: list[str] = []
            for granularity in ("hourly", "daily"):
                fields = [f"'{table}' AS tbl", f"'{granularity}' AS g", "COUNT(*) AS n"]
                for column in COLUMNS:
                    low, high = weather_range(column, granularity)
                    conds = [f"`{column}` IS NULL"]
                    if low is not None:
                        conds.append(f"`{column}` < {low}")
                    if high is not None:
                        conds.append(f"`{column}` > {high}")
                    fields.append(f"SUM({' OR '.join(conds)}) AS bad_{column}")
                    fields.append(f"SUM(`{column}` IS NULL) AS null_{column}")
                selects.append(
                    "SELECT " + ", ".join(fields)
                    + f" FROM `{table}` WHERE granularity = '{granularity}'"
                )
            parts.append("\nUNION ALL\n".join(selects))
    return "\nUNION ALL\n".join(parts) + ";\n"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def report(rows: list[dict[str, str]], *, as_json: bool) -> int:
    import json

    total_rows = 0
    total_bad = 0
    per_column: dict[str, dict[str, int]] = {
        column: {"rows": 0, "bad": 0, "null": 0} for column in COLUMNS
    }
    per_table: list[dict[str, object]] = []
    for row in rows:
        n = int(row["n"])
        if n == 0:
            continue
        table_bad = 0
        for column in COLUMNS:
            bad = int(row[f"bad_{column}"] or 0)
            nulls = int(row[f"null_{column}"] or 0)
            table_bad += bad
            per_column[column]["rows"] += n
            per_column[column]["bad"] += bad
            per_column[column]["null"] += nulls
        total_rows += n
        total_bad += table_bad
        per_table.append(
            {
                "table": row["tbl"],
                "granularity": row["g"],
                "rows": n,
                "bad_rows": table_bad,
                "accuracy": round(1.0 - table_bad / n, 8),
            }
        )

    accuracy = 1.0 - total_bad / total_rows if total_rows else 1.0
    payload = {
        "spec_source": "powersdk.timeseries.WEATHER_COLUMN_RANGE + weather_range(daily)",
        "total_rows": total_rows,
        "bad_rows": total_bad,
        "accuracy": round(accuracy, 8),
        "threshold": 0.998,
        "passed": accuracy >= 0.998,
        "per_column": {
            column: {
                "rows": stats["rows"],
                "out_of_range_or_null": stats["bad"],
                "null": stats["null"],
                "accuracy": round(1.0 - stats["bad"] / stats["rows"], 8) if stats["rows"] else 1.0,
                "bounds_hourly": list(WEATHER_COLUMN_RANGE[column]),
            }
            for column, stats in per_column.items()
        },
        "per_table": per_table,
    }

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"审计行数 {total_rows:,} · 越界/空值 {total_bad:,} · 准确率 {accuracy:.6%}")
    print(f"阈值 0.998 → {'达标' if payload['passed'] else '未达标'}")
    print(f"{'列':28}{'行数':>12}  {'越界+空值':>10}  {'空值':>8}  {'准确率':>12}")
    for column, stats in per_column.items():
        if not stats["rows"]:
            continue
        acc = 1.0 - stats["bad"] / stats["rows"]
        print(f"{column:28}{stats['rows']:>12,}  {stats['bad']:>10,}"
              f"  {stats['null']:>8,}  {acc:>11.6%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真实数据准确率审计")
    ap.add_argument("--emit-sql", action="store_true", help="输出审计 SQL（打到 stdout）")
    ap.add_argument("--from-tsv", type=Path, help="读入 --batch 跑出来的 TSV 结果并出报告")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.emit_sql:
        sys.stdout.write(emit_sql())
        return 0
    if args.from_tsv:
        return report(read_tsv(args.from_tsv), as_json=args.json)
    ap.error("需要 --emit-sql 或 --from-tsv")


if __name__ == "__main__":
    raise SystemExit(main())
