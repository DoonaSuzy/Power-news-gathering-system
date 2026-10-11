"""CSV 导出。

要点：

- 文件头写 UTF-8 BOM，Excel 双击不乱码；
- 列顺序固定，便于下游脚本按列名消费；
- 复用 ``query.build_where``，保证导出的筛选语义与 ``query`` 命令一致；
- 流式写出，避免整表读进内存。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from newsagg.cleaning import csv_cell
from newsagg.db import Database
from newsagg.logutil import get_logger
from newsagg.query import ArticleFilter, build_where

log = get_logger("export")

__all__ = ["EXPORT_COLUMNS", "iter_articles", "export_csv"]

#: 导出列（中文表头，直接给业务方看）
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "编号"),
    ("province_name", "省份"),
    ("province_code", "行政区划代码"),
    ("category", "来源类别"),
    ("source_name", "来源"),
    ("title", "标题"),
    ("body_text", "正文"),
    ("published_at_cst", "发布时间"),
    ("time_precision", "时间精度"),
    ("url", "原文链接"),
    ("quality_flags", "质量标记"),
)


def iter_articles(f: ArticleFilter, db: Database | None = None,
                  *, batch: int = 500) -> Iterator[dict[str, Any]]:
    """按批次流式读取文章。"""
    db = db or Database()
    where, params = build_where(f)
    offset = 0

    sql = (
        "SELECT a.id, p.name AS province_name, s.province_code, s.category, "
        "s.name AS source_name_full, a.title, a.body_text, a.published_at_cst, "
        "a.time_precision, a.url, a.quality_flags "
        "FROM articles a JOIN sources s ON s.id = a.source_id "
        "LEFT JOIN provinces p ON p.code = s.province_code "
        f"{where} ORDER BY a.published_at_cst DESC, a.id DESC LIMIT %s OFFSET %s"
    )

    while True:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, [*params, batch, offset])
                rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            return
        for row in rows:
            yield dict(row)
        if len(rows) < batch:
            return
        offset += batch


def export_csv(f: ArticleFilter, out_path: str | Path,
               db: Database | None = None) -> dict[str, Any]:
    """导出为 CSV，返回 ``{"path", "rows", "bytes"}``。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _key, label in EXPORT_COLUMNS])
        for article in iter_articles(f, db):
            # 兼容两套别名：source_name（导出列名）/ source_name_full（查询列名）
            if not article.get("source_name"):
                article["source_name"] = article.get("source_name_full") or ""
            if isinstance(article.get("published_at_cst"), datetime):
                article["published_at_cst"] = article["published_at_cst"].strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            writer.writerow([csv_cell(article.get(key)) for key, _label in EXPORT_COLUMNS])
            rows += 1

    size = path.stat().st_size
    log.info("导出完成: %s（%d 行，%.1f KB）", path, rows, size / 1024)
    return {"path": str(path), "rows": rows, "bytes": size}
