"""数据库连接工厂与初始化（建库、建表、种子数据）。

设计约定：**本模块只负责连接与 DDL/种子，业务 SQL 一律写在 ``repo.py``。**

``init-db`` 的完整动作：
1. 建库（若不存在，utf8mb4）
2. 执行 ``sql/schema.sql`` 建表
3. 灌入 34 个省级行政区（``provinces``）
4. 灌入 39 条电力新闻来源（``sources``）与省份槽位（``source_slots``）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from newsagg.config import Settings, get_settings
from newsagg.errors import ConfigError
from newsagg.logutil import get_logger
from newsagg.provinces import PROVINCES
from newsagg.sources_seed import SOURCES, list_url

log = get_logger("db")

__all__ = ["Database", "split_statements"]

#: 注释行与空行
_COMMENT_RE = re.compile(r"^\s*(--|#)")


def split_statements(sql: str) -> list[str]:
    """把 SQL 文件拆成单条语句（处理引号与注释，够用且不引第三方依赖）。"""
    statements: list[str] = []
    buf: list[str] = []
    in_single = in_double = in_backtick = False
    i = 0
    length = len(sql)

    while i < length:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if not (in_single or in_double or in_backtick):
            if ch == "-" and nxt == "-":
                while i < length and sql[i] != "\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i < length and not (sql[i] == "*" and i + 1 < length and sql[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            if ch == "#":
                while i < length and sql[i] != "\n":
                    i += 1
                continue

        if ch == "'" and not (in_double or in_backtick):
            in_single = not in_single
        elif ch == '"' and not (in_single or in_backtick):
            in_double = not in_double
        elif ch == "`" and not (in_single or in_double):
            in_backtick = not in_backtick
        elif ch == ";" and not (in_single or in_double or in_backtick):
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class Database:
    """MySQL 连接工厂。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------------- 连接 ----------------
    def connect(self, *, database: str | None = None, autocommit: bool = False):
        """返回一个新的 PyMySQL 连接（DictCursor）。"""
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("缺少 PyMySQL，请先 pip install -r requirements.txt") from exc

        params = dict(self.settings.dsn)
        if database is not None:
            params["database"] = database
        params["autocommit"] = autocommit
        return pymysql.connect(cursorclass=DictCursor, **params)

    # ---------------- 初始化 ----------------
    def ensure_database(self) -> str:
        """建库（若不存在）。返回库名。"""
        name = self.settings.mysql_database
        conn = self.connect(database="")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    f"DEFAULT CHARACTER SET {self.settings.mysql_charset} "
                    "DEFAULT COLLATE utf8mb4_general_ci"
                )
            conn.commit()
        finally:
            conn.close()
        return name

    def run_sql_file(self, path: str | Path) -> int:
        """执行 SQL 文件，返回执行的语句条数。"""
        sql_path = Path(path)
        if not sql_path.exists():
            raise ConfigError(f"SQL 文件不存在: {sql_path}")
        statements = split_statements(sql_path.read_text(encoding="utf-8"))
        conn = self.connect()
        executed = 0
        try:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
                    executed += 1
            conn.commit()
        finally:
            conn.close()
        return executed

    def ensure_schema(self) -> int:
        """建表：执行 ``sql/schema.sql``。"""
        schema = self.settings.sql_dir / "schema.sql"
        executed = self.run_sql_file(schema)
        log.info("schema 已就绪（执行 %d 条语句）", executed)
        return executed

    def seed_provinces(self) -> int:
        """灌入 34 个省级行政区，返回写入/更新条数。"""
        rows = [(p.code, p.name, p.short, p.region, int(p.is_special)) for p in PROVINCES]
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO provinces (code, name, short_name, region, is_special) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), short_name=VALUES(short_name), "
                    "region=VALUES(region), is_special=VALUES(is_special)",
                    rows,
                )
                affected = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        log.info("provinces 已灌入 %d 条", len(rows))
        return affected

    def seed_sources(self) -> int:
        """灌入 39 条来源 + 省份槽位。"""
        conn = self.connect()
        written = 0
        try:
            with conn.cursor() as cur:
                for src in SOURCES:
                    cur.execute(
                        "INSERT INTO sources "
                        "(adapter_name, source_id, name, province_code, base_url, "
                        " news_path, list_url, category, priority, crawl_interval_hours, "
                        " enabled, note) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE "
                        " name=VALUES(name), province_code=VALUES(province_code), "
                        " base_url=VALUES(base_url), news_path=VALUES(news_path), "
                        " list_url=VALUES(list_url), category=VALUES(category), "
                        " priority=VALUES(priority), "
                        " crawl_interval_hours=VALUES(crawl_interval_hours), "
                        " note=VALUES(note)",
                        (
                            src["id"], src["id"], src["name"], src["province_code"],
                            src["base_url"], src["news_path"], list_url(src),
                            src["category"], src["priority"], src["interval_hours"],
                            1 if src["enabled"] else 0, src["note"],
                        ),
                    )
                    written += 1
                    # 省份槽位：每个省级行政区一个启用槽（港澳台同样占位）
                    if src["province_code"]:
                        cur.execute(
                            "INSERT INTO source_slots (province_code, adapter_name) "
                            "VALUES (%s,%s) ON DUPLICATE KEY UPDATE adapter_name=adapter_name",
                            (src["province_code"], src["id"]),
                        )
            conn.commit()
        finally:
            conn.close()
        log.info("sources 已灌入 %d 条", written)
        return written

    def init(self) -> dict[str, int]:
        """完整初始化：建库 → 建表 → 种子。"""
        self.ensure_database()
        executed = self.ensure_schema()
        self.seed_provinces()
        sources = self.seed_sources()
        return {"statements": executed, "sources": sources, "provinces": len(PROVINCES)}

    # ---------------- 健康检查 ----------------
    def check(self) -> dict[str, Any]:
        """连通性与表清单检查。"""
        result: dict[str, Any] = {"ok": False, "tables": [], "error": ""}
        try:
            conn = self.connect()
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE() AS db, VERSION() AS ver")
                row = cur.fetchone() or {}
                result["database"] = row.get("db")
                result["version"] = row.get("ver")
                cur.execute("SHOW TABLES")
                result["tables"] = sorted(
                    next(iter(r.values())) for r in cur.fetchall()
                )
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
        return result

    def executemany(self, sql: str, rows: Iterable[tuple]) -> int:
        """批量执行（供脚本使用）。"""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, list(rows))
                affected = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return affected
