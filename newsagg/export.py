"""CSV 导出。

要点：

- 文件头写 UTF-8 BOM，Excel 双击不乱码；
- 列顺序固定，便于下游脚本按列名消费；
- 复用 ``query.build_where``，保证导出的筛选语义与 ``query`` 命令一致；
- 流式写出，避免整表读进内存。
"""

from __future__ import annotations

import csv
import json
import re
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

#: 采集批次元信息列（**追加在** :data:`EXPORT_COLUMNS` 之后）。
#:
#: 为什么不放在**首行**做注释行：那会破坏 CSV 结构 ——
#: 下游按第一行解析表头的脚本会直接把注释当成列名，全表错位。
#: 用户要求的是「在首行添加版本号、采集时间戳**字段**」，即**加列**：
#: 表头多两列、每行都带版本，既满足追溯，又不破坏任何解析器。
BATCH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("batch_version", "采集版本"),
    ("collected_at", "采集时间戳"),
)

#: 完整导出列 = 业务列 + 批次元信息列
ALL_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = EXPORT_COLUMNS + BATCH_COLUMNS

#: 毫秒级时间戳格式（用户指定，统一口径）
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"      # 配合 [:23] 截断到毫秒
TIMESTAMP_EXAMPLE = "2026-10-12 20:03:38.123"

#: 版本号里的压缩时间戳：``YYYYMMDD-HHMMSSmmm``
_VERSION_TIME = "%Y%m%d-%H%M%S%f"


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


def normalize_row(article: dict[str, Any]) -> dict[str, Any]:
    """把查询行整理成 :data:`EXPORT_COLUMNS` 要求的形状。

    **两处导出（手工 ``export_csv`` 与采集侧 ``export_job_csv``）共用**。
    早先这逻辑只写在 ``export_csv`` 里，采集侧新写的导出漏了它 ——
    结果 CSV 里「来源」列整列为空（查询列名是 ``source_name_full``，
    而导出列名是 ``source_name``），字段全对、内容却缺一列，**很难一眼看出**。

    处理两件事：

    1. ``source_name`` / ``source_name_full`` 是同一列的两种别名（历史原因），统一；
    2. ``datetime`` 格式化成 ``YYYY-MM-DD HH:MM:SS`` —— 直接交给 ``csv.writer``
       会写出 ``str(datetime)`` 的形式，与其它导出文件不一致。

    Returns:
        原地修改后的同一个 dict（便于链式调用）。
    """
    if not article.get("source_name"):
        article["source_name"] = article.get("source_name_full") or ""
    if isinstance(article.get("published_at_cst"), datetime):
        article["published_at_cst"] = article["published_at_cst"].strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return article


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
            article = normalize_row(article)
            writer.writerow([csv_cell(article.get(key)) for key, _label in EXPORT_COLUMNS])
            rows += 1

    size = path.stat().st_size
    log.info("导出完成: %s（%d 行，%.1f KB）", path, rows, size / 1024)
    return {"path": str(path), "rows": rows, "bytes": size}


# ==================== 采集任务 CSV（双输出的「文件侧」）====================

#: 采集任务 CSV 的默认根目录（相对项目根）。与 ``exports/``（手工导出）
#: 和 ``data/reports/``（分析报告）分开，避免混在一起难检索。
JOB_CSV_ROOT = "data/csv"

#: 不能直接进文件/目录名的字符。**按 Windows 的规则处理**（比 POSIX 严），
#: 「能跑在 Windows 上」就意味着「在 Linux 上也没问题」，反之不成立。
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_path_name(name: str, *, fallback: str = "unknown") -> str:
    """把来源名清洗成可安全用于目录/文件名的形式。

    Args:
        name: 原始来源名（可能含 ``/``、``:``、空格、中文）。
        fallback: 清洗后为空时的替代名。

    Returns:
        安全的路径片段；中文与空格**保留**（可读性优先），只替换非法字符。

    Note:
        清洗后若为空或只剩点，必须用 ``fallback`` —— 否则会生成 ``.`` 或 ``..``
        这类**会逃出目标目录**的名字（``mkdir('.')`` 静默成功，
        ``open('../x.csv')`` 写到上级目录，都属于难察觉的越界）。
    """
    cleaned = _UNSAFE_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(" .")          # Windows 不允许尾随空格与点
    if not cleaned or set(cleaned) <= {"_"}:
        return fallback
    return cleaned[:80]                     # 防超长（Windows 全路径 260 上限）


def export_job_csv(
    *,
    job_id: int,
    source_id: int,
    source_name: str = "",
    db: Database | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """把**某个采集 job 入库的文章**导出为 CSV。

    目录结构（按「站点 → 日期」二级分类，便于按站检索与按日回溯）::

        <root>/<来源名>/<YYYY-MM-DD>/<来源名>-<YYYYMMDD-HHMMSS>.csv

    举例::

        data/csv/三峡集团/2026-10-12/三峡集团-20261012-194500.csv

    与数据库是**双输出**关系：本函数**只读库、不写库**，
    因此它失败不会影响入库结果 —— 见 :func:`newsagg.crawl.engine.conclude`
    里的容错调用。

    Args:
        job_id: 采集任务 id（``crawl_jobs.id``）。
        source_id: 来源 id，与 ``job_id`` 一起用于精确定位本次入库的文章。
        source_name: 来源**显示名**（用中文站名做目录名，便于人检索）。
            留空则自动从 ``sources.name`` 查 —— 采集侧只有 ``adapter_name``
            这种机器 id（如 ``ctg``），直接拿来当目录名对人没意义。
        db: 数据库实例，默认新建。
        root: CSV 根目录，默认 :data:`JOB_CSV_ROOT`（相对项目根）。
        now: 采集时间戳，默认当前时间（测试可注入）。

    Returns:
        ``{"path", "rows", "bytes"}``；该 job 没有入库文章时返回 ``None``
        （**不生成空文件** —— 空 CSV 会污染目录且让「有没有采到」难以判断）。

    Raises:
        OSError: 目录创建或写文件失败。
        Exception: 数据库查询失败。

    Note:
        **写入是原子的**：先写同名 ``.tmp`` 再 ``replace``。
        否则采集任务中途被杀会留下**半截 CSV**，而它看起来是个正常文件 ——
        下游按目录扫描时会读到残缺数据，比没有文件更危险。
    """
    db = db or Database()
    when = now or datetime.now()

    # 用 join article_tasks 精确定位「本次 job 入库的文章」：
    # articles 表没有 job_id，但 article_tasks 有 (job_id, discovered_url_hash)，
    # 而 discovered_url_hash 就是 articles.url_hash —— 即入库身份之一。
    sql = (
        "SELECT a.id, p.name AS province_name, s.province_code, s.category, "
        "s.name AS source_name_full, a.title, a.body_text, a.published_at_cst, "
        "a.time_precision, a.url, a.quality_flags "
        "FROM articles a "
        "JOIN sources s ON s.id = a.source_id "
        "JOIN article_tasks t "
        "  ON t.discovered_url_hash = a.url_hash AND t.source_id = a.source_id "
        "LEFT JOIN provinces p ON p.code = s.province_code "
        "WHERE t.job_id = %s AND t.source_id = %s AND t.status = 'done' "
        "ORDER BY a.published_at_cst DESC, a.id DESC"
    )

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (job_id, source_id))
            rows = [dict(r) for r in cur.fetchall()]

            if not source_name:
                # 采集侧传进来的是 adapter_name（机器 id）；目录名用中文站名更好检索
                cur.execute("SELECT name FROM sources WHERE id = %s", (source_id,))
                got = cur.fetchone()
                if got:
                    source_name = got["name"] if isinstance(got, dict) else got[0]
    finally:
        conn.close()

    if not rows:
        log.debug("job %s 无入库文章，跳过 CSV 生成", job_id)
        return None

    base = Path(root) if root else (Path(__file__).resolve().parents[1] / JOB_CSV_ROOT)
    safe_source = safe_path_name(
        source_name or f"source{source_id}", fallback=f"source{source_id}"
    )

    # 时间戳**对齐到毫秒**：要求的格式只到毫秒。若保留微秒，
    # 会出现「文件名的毫秒段」与「CSV 内时间戳」不一致的假象。
    when = when.replace(microsecond=(when.microsecond // 1000) * 1000)
    day = when.strftime("%Y-%m-%d")
    collected_at = format_timestamp(when)

    out_dir = base / safe_source / day
    out_dir.mkdir(parents=True, exist_ok=True)          # 目录不存在则建

    # 批次版本 = 毫秒时间戳 + 站点「当日」内自增序号。
    # 序号**扫目录**得出：采集是多进程的（supervisor 可跑 N 个 worker），
    # 进程内计数器各算各的、必然撞车 —— 文件系统才是唯一共享的真相来源。
    seq = next_batch_seq(out_dir, day)
    version = build_batch_version(when, seq)
    path = out_dir / f"{safe_source}-{version}.csv"

    tmp = path.with_suffix(path.suffix + ".tmp")
    written = 0
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([label for _, label in ALL_EXPORT_COLUMNS])
            for row in rows:
                article = normalize_row(dict(row))
                cells = [csv_cell(article.get(k)) for k, _ in EXPORT_COLUMNS]
                # 批次元信息**每行都写**，而不是只在首行加注释行 ——
                # 注释行会被下游当成表头，导致整表错位。
                cells += [version, collected_at]
                writer.writerow(cells)
                written += 1
        tmp.replace(path)               # 原子替换：要么完整文件，要么什么都没有
    except Exception:
        tmp.unlink(missing_ok=True)     # 失败时清掉半截临时文件，不留垃圾
        raise

    size = path.stat().st_size

    # 版本映射**双记录**：
    #   日志 —— 实时可观测，但会被轮转；
    #   索引文件 —— append-only，可长期按版本号反查。
    log.info("job %s 已导出 CSV：版本 %s，%d 行 → %s（%.1f KB）",
             job_id, version, written, path, size / 1024)
    append_version_index(
        {
            "version": version,
            "job_id": job_id,
            "source_id": source_id,
            "source_name": source_name,
            "collected_at": collected_at,
            "rows": written,
            "bytes": size,
            "path": str(path),
        },
        root=base,
    )

    return {
        "path": str(path), "rows": written, "bytes": size,
        "version": version, "collected_at": collected_at,
    }


# ==================== 采集批次版本标识 ====================

#: 版本号形态：``v<YYYYMMDD>-<HHMMSSmmm>-<NNNN>``
#:
#: - 时间戳段到**毫秒**，跨批次天然不冲突；
#: - 序号段是**站点 + 当日**的自增序列，用于同一毫秒内的极端并发
#:   （以及让「同一天采了几次」一眼可见）。
_VERSION_RE = re.compile(r"-v(\d{8})-(\d{9})-(\d{4})\.csv$")


def format_timestamp(when: datetime) -> str:
    """毫秒级时间戳，格式 ``YYYY-MM-DD HH:mm:ss.SSS``。

    ``strftime('%f')`` 给的是**微秒**（6 位），截前 3 位得到毫秒。
    直接写 ``%f`` 会得到 6 位小数，与用户指定的格式不符。
    """
    return when.strftime(TIMESTAMP_FORMAT)[:23]


def build_batch_version(when: datetime, seq: int) -> str:
    """拼出批次版本号：``v<YYYYMMDD>-<HHMMSSmmm>-<NNNN>``。

    Args:
        when: 采集时间。
        seq: 该站点当日的序号（从 1 开始）。

    Returns:
        形如 ``v20261012-200338123-0001`` 的版本号。

    Example:
        >>> build_batch_version(datetime(2026, 10, 12, 20, 3, 38, 123000), 1)
        'v20261012-200338123-0001'
    """
    # ``%Y%m%d-%H%M%S%f`` 格式化后是 ``20261012-200547061000``（``%f`` 是 6 位微秒）。
    # 取前 18 位 = ``20261012``(8) + ``-``(1) + ``200547``(6) + ``061``(3) —— 正好到毫秒。
    # ⚠️ 曾经写成 ``[:19]``，多带了一位微秒 → 时间戳段变成 10 位，
    #    于是 ``_VERSION_RE``（按 9 位匹配）再也匹配不上，
    #    ``next_batch_seq`` 恒返回 1，序号永远停在 ``0001``。
    stamp = when.strftime(_VERSION_TIME)[:18]
    return f"v{stamp[:8]}-{stamp[9:]}-{seq:04d}"


def next_batch_seq(out_dir: Path, day: str) -> int:
    """扫目录算出该站点当日的下一个序号。

    **为什么扫目录而不是用进程内计数器**：采集是多进程的
    （``supervisor`` 可跑 N 个 worker），进程内计数器各算各的、必然撞车；
    而文件系统是所有 worker 共享的**唯一真相来源**。

    只统计 ``<站点名>/<YYYY-MM-DD>/`` 这一个目录下的文件 ——
    序号是「站点 + 当日」维度，不同站点、不同自然日各自从 1 开始。

    Returns:
        下一个可用序号（目录不存在或无文件时返回 1）。
    """
    if not out_dir.exists():
        return 1
    max_seq = 0
    # 用 ``*.csv`` 粗筛 + 正则精筛，**不要在 glob 里拼日期** ——
    # 曾经写成 ``*-{YYYYMMDD}-*.csv``，漏掉了版本号前缀的 ``v``
    # （实际文件名是 ``-v20261012-``），于是 glob 恒匹配 0 个、
    # 序号永远停在 1。glob 只负责缩小范围，精确判定交给正则。
    for path in out_dir.glob("*.csv"):
        m = _VERSION_RE.search(path.name)
        if m and m.group(1) == day.replace("-", ""):
            max_seq = max(max_seq, int(m.group(3)))
    return max_seq + 1


#: 版本映射索引文件（append-only JSONL）。一行一次导出，便于反查「版本 → 文件」。
VERSION_INDEX_NAME = "_versions.jsonl"


def append_version_index(entry: dict[str, Any], *, root: Path | None = None) -> None:
    """把一次导出的版本映射追加到索引文件。

    与采集日志（``log.info``）**双记录**：

    - 日志：实时可观测，但会被轮转、不便长期反查；
    - 索引文件：append-only，可长期保留、可被脚本按版本号检索。

    Args:
        entry: 至少含 ``version`` / ``path`` / ``rows`` / ``collected_at``。
        root: 索引所在根目录，默认 :data:`JOB_CSV_ROOT`。

    Note:
        本函数**吞掉自己的异常**（仅记 warning）：索引是辅助产物，
        它的失败不该让已经成功落盘的 CSV 被判定为失败。
    """
    base = root or (Path(__file__).resolve().parents[1] / JOB_CSV_ROOT)
    try:
        base.mkdir(parents=True, exist_ok=True)
        index = base / VERSION_INDEX_NAME
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - 索引失败不影响 CSV 结果
        log.warning("写版本索引失败（CSV 已落盘，不影响结果）：%s", exc)


def read_version_index(*, root: Path | None = None) -> list[dict[str, Any]]:
    """读回版本索引，按写入顺序返回。坏行跳过，不抛异常。"""
    base = root or (Path(__file__).resolve().parents[1] / JOB_CSV_ROOT)
    index = base / VERSION_INDEX_NAME
    if not index.exists():
        return []
    out: list[dict[str, Any]] = []
    with index.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # 半截行（进程被杀）跳过，不让它毁掉整个索引
    return out
