"""原始响应落盘层（``newsagg/rawstore.py``）的回归测试。

## 这层解决什么问题

当前采集链路「解析 → 清洗 → 入库」**不存成功的原始响应**，导致：

- 改解析规则只能**重抓**（`scripts/repair_bjx_bodies.py` 的处境）
- 站点改版/下线后历史数据**永久定格**
- 排查「这条为什么没采到」时无据可比

有了原始层，改规则只需**重跑解析**（`scripts/rewash.py`）。

## 测试重点

1. **默认关闭** —— 长期运行且无人归档时磁盘会无声涨满
2. **原子写** —— 不留半截文件
3. **同名不覆盖** —— 同一 URL 不同时刻的内容是不同的快照
4. **吞掉自己的异常** —— 原始层是"后悔药"，失败不该让采集失败
5. **路径清洗复用** —— 曾复制一份并写反判断，导致文件名全变 `nohash`
"""

from __future__ import annotations

import gzip
from datetime import datetime

import pytest

from newsagg.export import safe_path_name
from newsagg.rawstore import RawStore, iter_raw, raw_store_for

# ======================================================================
# 1. 开关语义
# ======================================================================


def test_disabled_by_default(tmp_path):
    """默认**关闭** —— 长期采集若无人归档，磁盘会无声涨满。"""
    store = RawStore(tmp_path, source_id="ctg")
    assert store.enabled is False
    assert store.save("https://x/y", "abc123", b"<html/>") is None
    assert not list(tmp_path.rglob("*.gz")), "未启用时不该写任何文件"


def test_env_var_controls_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("POWERNEWS_KEEP_RAW", raising=False)
    assert raw_store_for("ctg", base_dir=tmp_path).enabled is False

    for on in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("POWERNEWS_KEEP_RAW", on)
        assert raw_store_for("ctg", base_dir=tmp_path).enabled is True, f"{on!r} 应视为开启"

    monkeypatch.setenv("POWERNEWS_KEEP_RAW", "0")
    assert raw_store_for("ctg", base_dir=tmp_path).enabled is False


# ======================================================================
# 2. 落盘与回读
# ======================================================================


def _enabled(tmp_path) -> RawStore:
    return RawStore(tmp_path, source_id="ctg", enabled=True)


def test_save_and_load_roundtrip(tmp_path):
    store = _enabled(tmp_path)
    body = "<html><body>中文正文</body></html>".encode()
    path = store.save("https://a/b", "deadbeef", body)

    assert path is not None and path.exists()
    assert store.load(path) == body, "读回内容应与写入完全一致（字节级）"


def test_saved_as_gzip(tmp_path):
    """必须是 gzip —— HTML 通常是正文的 10~50 倍，不压缩会吃光磁盘。"""
    store = _enabled(tmp_path)
    path = store.save("https://a/b", "deadbeef", b"<html/>" * 1000)
    with gzip.open(path, "rb") as fh:          # 能被 gzip 打开即证明格式正确
        assert fh.read() == b"<html/>" * 1000


def test_directory_layout_is_source_then_day(tmp_path):
    """目录按「站点 → 日期」两级 —— 与 CSV 输出层保持同一检索习惯。"""
    store = _enabled(tmp_path)
    path = store.save("https://a/b", "h" * 40, b"x", when=datetime(2026, 10, 12))
    assert path.parent.name == "2026-10-12"
    assert path.parent.parent.name == "ctg"


def test_filename_uses_url_hash(tmp_path):
    """文件名应是 url_hash —— **回归**：曾因路径清洗写反判断，
    40 位 hex 全部退化成 `nohash`，于是一批文件挤在同一个名字上。"""
    store = _enabled(tmp_path)
    h = "65fd6ec62ad6c998ca8c4060d60011db89fc1f03"
    path = store.save("https://a/b", h, b"x")
    assert path.name == f"{h}.html.gz"


def test_url_and_meta_sidecar_written(tmp_path):
    """侧车文件记 URL 与状态码 —— 正文里认不出原始地址，重洗时要靠它。"""
    store = _enabled(tmp_path)
    path = store.save("https://a/b?c=d", "h" * 40, b"x", status=404)

    assert path.with_suffix(".url").read_text(encoding="utf-8") == "https://a/b?c=d"
    meta = path.with_suffix(".meta").read_text(encoding="utf-8")
    assert "status=404" in meta and "collected_at=" in meta


def test_non_200_is_also_saved(tmp_path):
    """失败响应同样有价值（排查「为什么没采到」时要比对当时的返回）。"""
    store = _enabled(tmp_path)
    path = store.save("https://a/b", "h" * 40, b"challenge", status=412)
    assert path is not None and path.exists()


# ======================================================================
# 3. 同名不覆盖（快照语义）
# ======================================================================


def test_same_hash_does_not_overwrite(tmp_path):
    """同一 URL 在不同时刻的内容可能不同（站点更新了稿件）——
    覆盖会丢掉旧版本这个「快照」价值。"""
    store = _enabled(tmp_path)
    when = datetime(2026, 10, 12, 10, 0, 0)

    p1 = store.save("https://a/b", "h" * 40, b"first", when=when)
    p2 = store.save("https://a/b", "h" * 40, b"second", when=when)

    assert p1 != p2, "第二次落盘应换名，不能覆盖第一次"
    assert store.load(p1) == b"first", "旧快照必须完好"
    assert store.load(p2) == b"second"


# ======================================================================
# 4. 异常容错（核心：原始层失败不得影响采集）
# ======================================================================


def test_save_swallows_errors(tmp_path, monkeypatch):
    """落盘失败**只记 warning**，返回 None。

    回归思路：若让异常冒泡，会出现「CSV/原文写失败导致任务被判失败」——
    而入库已经成功了，重试纯属浪费。
    """
    store = _enabled(tmp_path)

    def boom(*_a, **_k):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(gzip, "open", boom)
    assert store.save("https://a/b", "h" * 40, b"x") is None, "应吞掉异常返回 None"


def test_no_tmp_left_on_failure(tmp_path, monkeypatch):
    """失败时**不留半截 .tmp** —— 半截文件看着正常，下游读会拿到残缺数据。"""
    store = _enabled(tmp_path)
    real_replace = type(tmp_path).replace

    def boom(self, *_a, **_k):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(type(tmp_path), "replace", boom, raising=False)
    store.save("https://a/b", "h" * 40, b"x")
    monkeypatch.setattr(type(tmp_path), "replace", real_replace, raising=False)

    assert list(tmp_path.rglob("*.tmp")) == [], "失败后残留了 .tmp"


# ======================================================================
# 5. 路径清洗复用（防止再次分叉）
# ======================================================================


@pytest.mark.parametrize(("raw", "expected"), [
    ("65fd6ec62ad6c998ca8c4060d60011db89fc1f03", "65fd6ec62ad6c998ca8c4060d60011db89fc1f03"),
    ("gov_gz_nyj", "gov_gz_nyj"),
    ("cr_power", "cr_power"),
    ("sgcc_sc", "sgcc_sc"),
])
def test_path_name_keeps_underscore_free_hashes(raw, expected):
    """**核心回归**：不含下划线的名字（40 位 hex）必须原样保留。

    曾经手写了 ``set(cleaned) > {"_"}``（真超集）——
    任何不含下划线的名字都不满足 ``⊇ {"_"}``，于是**全部退化**成 fallback。
    而含下划线的 `gov_gz_nyj` 恰好通过，掩盖了 bug。
    """
    assert safe_path_name(raw) == expected


@pytest.mark.parametrize("dangerous", ["", "   ", ".", "..", "___"])
def test_path_name_falls_back_for_dangerous_names(dangerous):
    """空/点/纯下划线必须回落 —— 否则 `.` / `..` 会逃出目标目录。"""
    assert safe_path_name(dangerous, fallback="FB") == "FB"


# ======================================================================
# 6. 遍历（重洗工具的输入）
# ======================================================================


def test_iter_raw_walks_all(tmp_path):

    for sid in ("ctg", "cr_power"):
        for day in (1, 2):
            RawStore(tmp_path, source_id=sid, enabled=True).save(
                f"https://{sid}/{day}", f"{sid}{day}".ljust(40, "0"), b"x",
                when=datetime(2026, 10, day),
            )
    records = list(iter_raw(tmp_path))
    assert len(records) == 4
    assert {r.source_id for r in records} == {"ctg", "cr_power"}


def test_iter_raw_filters_by_source_and_day(tmp_path):
    for sid in ("ctg", "cr_power"):
        for day in (1, 2):
            RawStore(tmp_path, source_id=sid, enabled=True).save(
                f"https://{sid}/{day}", f"{sid}{day}".ljust(40, "0"), b"x",
                when=datetime(2026, 10, day),
            )
    assert len(list(iter_raw(tmp_path, source_id="ctg"))) == 2
    assert len(list(iter_raw(tmp_path, day="2026-10-01"))) == 2
    assert len(list(iter_raw(tmp_path, source_id="ctg", day="2026-10-01"))) == 1


def test_iter_raw_on_missing_dir_is_empty(tmp_path):
    """目录不存在时返回空迭代器，**不抛异常** —— 首次使用是常态。"""
    assert list(iter_raw(tmp_path / "不存在")) == []


def test_record_url_reads_sidecar(tmp_path):
    store = _enabled(tmp_path)
    store.save("https://a/b?q=1", "h" * 40, b"x")
    rec = next(iter(iter_raw(tmp_path)))
    assert rec.url == "https://a/b?q=1"
