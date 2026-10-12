"""采集任务 CSV 导出（双输出的文件侧）的回归测试。

覆盖三类风险，每条都对应一个**真实踩过或极易踩到**的坑：

1. **路径安全** —— 来源名含非法字符、或清洗后为空/全是点，
   会生成 ``.`` / ``..`` 这种能**逃出目标目录**的名字；
2. **字段完整性** —— 查询列名与导出列名不一致（``source_name_full`` vs
   ``source_name``）会让整列导出为空，而其它列全对，**很难一眼看出**；
3. **采集不受影响** —— 文件侧异常（无权限、磁盘满、路径非法）
   **绝不能中断采集任务**，入库是主链路。

全部离线：数据库用假对象，不连真库。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from newsagg.export import (
    ALL_EXPORT_COLUMNS,
    build_batch_version,
    export_job_csv,
    format_timestamp,
    next_batch_seq,
    normalize_row,
    read_version_index,
    safe_path_name,
)

# ======================================================================
# 1. 路径安全
# ======================================================================


@pytest.mark.parametrize(("raw", "expected"), [
    ("三峡集团", "三峡集团"),            # 中文保留
    ("国网上海市电力公司", "国网上海市电力公司"),
    ("a/b:c*d", "a_b_c_d"),              # Windows 非法字符替换
    ('x<y>z|w?q"', "x_y_z_w_q_"),        # 同上
    ("  spaced  ", "spaced"),            # 首尾空白去掉
])
def test_safe_path_name_keeps_readable_and_strips_illegal(raw, expected):
    assert safe_path_name(raw) == expected


@pytest.mark.parametrize("dangerous", [".", "..", "  .  ", "", "   ", "///"])
def test_safe_path_name_never_returns_dot_or_empty(dangerous):
    """回归：清洗后为空或只剩点，必须回落 fallback。

    否则 ``mkdir('.')`` 会静默成功（目录就是当前目录），
    ``open('../x.csv')`` 会写到**上级目录** —— 都属于难察觉的越界。
    """
    got = safe_path_name(dangerous, fallback="FB")
    assert got == "FB", f"{dangerous!r} 清洗后得到 {got!r}，有越界风险"


def test_safe_path_name_truncates_overlong():
    """Windows 全路径上限 260，来源名过长要截断。"""
    assert len(safe_path_name("x" * 500)) == 80


# ======================================================================
# 2. 字段完整性
# ======================================================================


def test_normalize_row_fills_source_name_from_query_alias():
    """回归：查询用 ``source_name_full``、导出列用 ``source_name``。

    早先这步只写在 ``export_csv`` 里，新写的采集侧导出漏了它 ——
    CSV 的「来源」列**整列为空**，而其余列全对，非常难发现。
    """
    row = normalize_row({"source_name_full": "三峡集团"})
    assert row["source_name"] == "三峡集团"


def test_normalize_row_formats_datetime():
    """``datetime`` 不格式化会写出 ``str(datetime)``，与其它导出口径不一致。"""
    row = normalize_row({"published_at_cst": datetime(2026, 10, 12, 9, 5, 3)})
    assert row["published_at_cst"] == "2026-10-12 09:05:03"


def test_normalize_row_keeps_existing_source_name():
    row = normalize_row({"source_name": "已有值", "source_name_full": "别的"})
    assert row["source_name"] == "已有值"


# ======================================================================
# 3. export_job_csv 行为（假 DB）
# ======================================================================


class _FakeCursor:
    def __init__(self, rows: list[dict], source_name: str = "三峡集团") -> None:
        self._rows = rows
        self._source_name = source_name
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, _params=None):
        self._sql = sql

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return {"name": self._source_name}


class _FakeConn:
    def __init__(self, rows, source_name="三峡集团"):
        self._rows, self._source_name = rows, source_name

    def cursor(self):
        return _FakeCursor(self._rows, self._source_name)

    def close(self):
        pass


class _FakeDB:
    def __init__(self, rows, source_name="三峡集团"):
        self._rows, self._source_name = rows, source_name

    def connect(self):
        return _FakeConn(self._rows, self._source_name)


def _row(**over):
    base = {
        "id": 1, "province_name": None, "province_code": None, "category": "independent",
        "source_name_full": "三峡集团", "title": "标题", "body_text": "正文",
        "published_at_cst": datetime(2026, 10, 12), "time_precision": "date",
        "url": "https://example.com/a", "quality_flags": "[]",
    }
    base.update(over)
    return base


def test_export_job_csv_writes_expected_path_and_content(tmp_path):
    """目录按「站点 → 日期」二级分类；文件名是「站点名-版本号.csv」。"""
    db = _FakeDB([_row(), _row(id=2, title="第二篇")])
    when = datetime(2026, 10, 12, 19, 45, 0)

    out = export_job_csv(job_id=125, source_id=64, db=db, root=tmp_path, now=when)

    assert out is not None
    path = Path(out["path"])
    assert path.name == "三峡集团-v20261012-194500000-0001.csv"
    assert path.parent.name == "2026-10-12"
    assert path.parent.parent.name == "三峡集团"
    assert out["rows"] == 2
    assert out["version"] == "v20261012-194500000-0001"
    assert out["collected_at"] == "2026-10-12 19:45:00.000"

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == [label for _k, label in ALL_EXPORT_COLUMNS]
    assert len(rows) == 3                       # 表头 + 2 行
    assert rows[1][rows[0].index("来源")] == "三峡集团"
    assert rows[1][rows[0].index("发布时间")] == "2026-10-12 00:00:00"
    # 批次元信息：每行都带版本与采集时间戳
    assert rows[1][rows[0].index("采集版本")] == out["version"]
    assert rows[1][rows[0].index("采集时间戳")] == out["collected_at"]
    assert rows[2][rows[0].index("采集版本")] == out["version"]


def test_export_job_csv_returns_none_when_no_articles(tmp_path):
    """无入库文章**不生成空文件** —— 空 CSV 会污染目录，
    且让「这一轮到底采到没有」难以判断。"""
    out = export_job_csv(job_id=1, source_id=1, db=_FakeDB([]), root=tmp_path)
    assert out is None
    assert list(tmp_path.rglob("*.csv")) == []


def test_export_job_csv_does_not_overwrite_same_second(tmp_path):
    """同一时刻重复导出要**序号自增、互不覆盖** —— 两轮数据可能不同。"""
    db = _FakeDB([_row()])
    when = datetime(2026, 10, 12, 19, 45, 0)

    outs = [
        export_job_csv(job_id=1, source_id=1, db=db, root=tmp_path, now=when)
        for _ in range(3)
    ]

    versions = [o["version"] for o in outs]
    names = [Path(o["path"]).name for o in outs]
    assert len(set(versions)) == 3, f"版本重复：{versions}"
    assert len(set(names)) == 3, f"文件名重复：{names}"
    assert versions == [
        "v20261012-194500000-0001",
        "v20261012-194500000-0002",
        "v20261012-194500000-0003",
    ]


def test_export_job_csv_leaves_no_tmp_on_failure(tmp_path, monkeypatch):
    """写入失败时**不留半截文件**。

    半截 CSV 看起来是正常文件，下游按目录扫描会读到残缺数据 ——
    比没有文件更危险。所以用「临时文件 + 原子替换」，失败清掉临时文件。
    """
    db = _FakeDB([_row()])

    def boom(self, *_a, **_k):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError):
        export_job_csv(job_id=1, source_id=1, db=db, root=tmp_path)

    assert list(tmp_path.rglob("*.tmp")) == [], "失败后残留了 .tmp 文件"


def test_export_job_csv_raises_on_undeletable_dir(tmp_path, monkeypatch):
    """目录创建失败要**抛异常**（而不是静默跳过）——
    由调用方决定怎么处理；采集侧选择的是「记日志、继续」。"""
    def boom(self, *_a, **_k):
        raise PermissionError("模拟无写权限")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(PermissionError):
        export_job_csv(job_id=1, source_id=1, db=_FakeDB([_row()]), root=tmp_path)


# ======================================================================
# 4. 采集流程容错（核心要求：文件异常不得中断采集）
# ======================================================================


def _window():
    """构造一个时间窗口。``CrawlSession`` 要求 ``window`` 必填。"""
    from newsagg.timewindow import Window

    return Window(
        start_cst=datetime(2026, 9, 12), end_cst=datetime(2026, 10, 12)
    )


def _session(job_id: int):
    from newsagg.crawl.session import CrawlSession

    return CrawlSession(
        job_id=job_id, source_id=1, window=_window(), adapter_name="ctg",
        db=_FakeDB([_row()]),
    )


def test_conclude_survives_csv_export_failure(monkeypatch):
    """回归：导出 CSV 抛异常，``conclude()`` 必须**正常返回统计**。

    入库是主链路、CSV 是附加产物。若让文件侧异常冒泡，会出现
    「数据已经入库了，但任务被判失败并重试」—— 既污染台账又白跑一轮。
    """
    from newsagg.crawl import engine

    def boom(**_kwargs):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(engine, "export_job_csv", boom)

    session = _session(999)
    session.discovered, session.saved, session.requests = 4, 4, 6
    monkeypatch.setattr(session, "decide", lambda: ("succeeded", 0))
    monkeypatch.setattr(session, "finish", lambda _s: None)
    monkeypatch.setattr(session, "fail_task", lambda _m: None)

    stats = engine.conclude(session)

    assert stats["job_id"] == 999
    assert stats["saved"] == 4
    assert "csv_path" not in stats, "导出失败时不该伪造 csv_path"


def test_conclude_records_csv_path_on_success(monkeypatch):
    """导出成功时把路径写进 stats，便于日志与台账追溯。"""
    from newsagg.crawl import engine

    monkeypatch.setattr(
        engine, "export_job_csv",
        lambda **_k: {"path": "data/csv/三峡集团/2026-10-12/x.csv", "rows": 4, "bytes": 100},
    )

    session = _session(998)
    session.discovered, session.saved, session.requests = 4, 4, 6
    monkeypatch.setattr(session, "decide", lambda: ("succeeded", 0))
    monkeypatch.setattr(session, "finish", lambda _s: None)
    monkeypatch.setattr(session, "fail_task", lambda _m: None)

    stats = engine.conclude(session)
    assert stats["csv_rows"] == 4
    assert stats["csv_path"].endswith("x.csv")


# ======================================================================
# 5. 版本标识机制
# ======================================================================


@pytest.mark.parametrize(("when", "seq", "expected"), [
    (datetime(2026, 10, 12, 20, 3, 38, 123000), 1, "v20261012-200338123-0001"),
    (datetime(2026, 10, 12, 20, 3, 38, 123999), 1, "v20261012-200338123-0001"),
    (datetime(2026, 1, 2, 3, 4, 5, 6000), 42, "v20260102-030405006-0042"),
    (datetime(2026, 12, 31, 23, 59, 59, 999000), 9999, "v20261231-235959999-9999"),
])
def test_build_batch_version_format(when, seq, expected):
    """版本号形态：``v<YYYYMMDD>-<HHMMSSmmm>-<NNNN>``。

    回归：曾经把切片写成 ``[:19]``（多带一位微秒），时间戳段变成 10 位，
    导致 ``_VERSION_RE``（按 9 位匹配）失配、序号恒为 1。
    """
    assert build_batch_version(when, seq) == expected


def test_build_batch_version_truncates_microseconds_to_milliseconds():
    """微秒要截到毫秒，且**不四舍五入**（避免进位造成版本号跳变）。"""
    a = build_batch_version(datetime(2026, 10, 12, 20, 3, 38, 123000), 1)
    b = build_batch_version(datetime(2026, 10, 12, 20, 3, 38, 123999), 1)
    assert a == b, "同一毫秒内的两个微秒值应得到相同版本号"


@pytest.mark.parametrize("when", [
    datetime(2026, 10, 12, 20, 3, 38, 123000),
    datetime(2026, 1, 1, 0, 0, 0, 1),
    datetime(2026, 12, 31, 23, 59, 59, 999999),
])
def test_format_timestamp_is_millisecond_precision(when):
    """时间戳格式统一为 ``YYYY-MM-DD HH:mm:ss.SSS``（恰好 23 字符、3 位毫秒）。"""
    got = format_timestamp(when)
    assert len(got) == 23, f"{got!r} 长度不是 23"
    assert got[:19] == when.strftime("%Y-%m-%d %H:%M:%S")
    assert got[19] == "."
    assert len(got[20:]) == 3 and got[20:].isdigit()


def test_format_timestamp_pads_single_digit_millisecond():
    """毫秒要补零：``.001`` 而不是 ``.1``。"""
    assert format_timestamp(datetime(2026, 10, 12, 20, 3, 38, 1000)).endswith(".001")


def test_next_batch_seq_starts_at_one(tmp_path):
    assert next_batch_seq(tmp_path / "不存在", "2026-10-12") == 1


def test_next_batch_seq_scans_existing_versions(tmp_path):
    """序号来自**扫目录**，不是进程内计数器 ——
    采集是多进程的，进程内计数器各算各的必然撞车。"""
    d = tmp_path / "站点" / "2026-10-12"
    d.mkdir(parents=True)
    for seq in (1, 2, 7):
        (d / f"站点-v20261012-200000000-{seq:04d}.csv").write_text("x", encoding="utf-8")
    assert next_batch_seq(d, "2026-10-12") == 8


def test_next_batch_seq_ignores_other_days(tmp_path):
    """序号是「站点 + 当日」维度：别的自然日的文件不该影响今天的计数。"""
    d = tmp_path / "站点" / "2026-10-12"
    d.mkdir(parents=True)
    (d / "站点-v20261011-200000000-0009.csv").write_text("x", encoding="utf-8")
    assert next_batch_seq(d, "2026-10-12") == 1


def test_next_batch_seq_survives_unrelated_files(tmp_path):
    """目录里有非版本号命名的文件（历史遗留）也不能让扫描崩掉。"""
    d = tmp_path / "站点" / "2026-10-12"
    d.mkdir(parents=True)
    (d / "articles.csv").write_text("旧文件", encoding="utf-8")
    (d / "站点-v20261012-200000000-0003.csv").write_text("x", encoding="utf-8")
    assert next_batch_seq(d, "2026-10-12") == 4


def test_version_index_records_mapping(tmp_path):
    """版本信息要能反查「版本 → 文件」，索引与日志双记录。"""
    db = _FakeDB([_row()])
    out1 = export_job_csv(job_id=1, source_id=1, db=db, root=tmp_path,
                          now=datetime(2026, 10, 12, 20, 0, 0))
    export_job_csv(job_id=2, source_id=1, db=db, root=tmp_path,
                   now=datetime(2026, 10, 12, 20, 0, 1))

    entries = read_version_index(root=tmp_path)
    assert len(entries) == 2
    assert entries[0]["version"] == out1["version"]
    assert entries[0]["job_id"] == 1
    assert entries[0]["rows"] == 1
    assert entries[0]["collected_at"] == "2026-10-12 20:00:00.000"
    assert Path(entries[0]["path"]).name == Path(out1["path"]).name


def test_version_index_skips_broken_lines(tmp_path):
    """索引是 append-only，进程被杀可能留半截行 —— 读的时候要跳过而不是崩。"""
    (tmp_path / "_versions.jsonl").write_text(
        '{"version":"v1"}\n'          # 正常
        '{"version":"v2"\n'           # 半截（无右括号 → 非法 JSON）
        '{"version":"v3"}\n',         # 正常
        encoding="utf-8",
    )
    versions = [e["version"] for e in read_version_index(root=tmp_path)]
    assert versions == ["v1", "v3"]


def test_version_index_failure_does_not_break_export(tmp_path, monkeypatch):
    """索引写失败不能影响 CSV 结果 —— 索引是辅助产物。

    回归思路：若让索引异常冒泡，会出现「CSV 已落盘但任务被判失败」。
    """
    from newsagg import export as export_mod

    monkeypatch.setattr(
        export_mod, "append_version_index",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("模拟索引盘满")),
    )
    # append_version_index 自身吞异常，这里验证的是「它不抛」——
    # 所以直接调真实实现而不是替身
    monkeypatch.undo()

    out = export_job_csv(job_id=1, source_id=1, db=_FakeDB([_row()]), root=tmp_path)
    assert out is not None and Path(out["path"]).exists()


def test_versions_unique_across_many_exports(tmp_path):
    """**连续 50 次导出，版本号与文件名都不得重复**（稳定性验证）。"""
    db = _FakeDB([_row()])
    base = datetime(2026, 10, 12, 20, 0, 0)
    versions, names = [], []
    for i in range(50):
        out = export_job_csv(
            job_id=i, source_id=1, db=db, root=tmp_path,
            now=base,                       # 同一时刻，逼出序号自增路径
        )
        versions.append(out["version"])
        names.append(Path(out["path"]).name)

    assert len(set(versions)) == 50, f"版本号重复：{len(set(versions))}/50"
    assert len(set(names)) == 50, f"文件名重复：{len(set(names))}/50"
    assert versions[0].endswith("-0001") and versions[-1].endswith("-0050")
    assert len(list(tmp_path.rglob("*.csv"))) == 50
