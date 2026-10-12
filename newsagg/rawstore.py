"""原始响应落盘层。

## 为什么需要它（这是本项目最大的架构缺口）

当前采集链路是「**解析 → 清洗 → 入库**」，**成功的原始响应不落盘**：

- `Sampler` 只存**失败的**样本（`save_failure`）
- 成功的详情页 HTML 用完即弃

后果（实测踩到）：

1. **解析规则改进后无法回溯重洗** —— 只能重新发起抓取。
   `scripts/repair_bjx_bodies.py` 就是被这个逼出来的：
   150+ 条记录的正文因 WAF 采空，改规则修不了，**必须用真实浏览器重抓**。
2. **站点改版/下线后，历史数据永久定格** —— 那些"清洗得不够好"的存量
   再也没有机会改进。
3. **排查无据** —— 「这条为什么没采到」时，没有当时的响应可比对。

有了原始层，链路变成：

    抓取 → 【原始响应落盘】→ 解析 → 清洗 → 入库
             ↑ 可重放、可重洗、可审计

## 存什么：原始 HTML，不是解析结果

**关键决策**：存**原始 HTML**（gzip 压缩），而不是解析后的中间结构。

理由：最常见的需求是「**站点改版后换选择器**」。若只存解析结果，
换选择器时**无济于事** —— 因为结构化数据里已经没有 DOM 信息了。
存 HTML 才能覆盖「换选择器 + 改清洗规则」两类回溯需求。

代价是体积大（HTML 通常是正文的 10~50 倍），用 gzip + 分级归档控制。

## 开关：默认关闭

采集是长期运行的，若默认开启而没人管归档，磁盘会**无声涨满**。
所以默认 `False`，需要时用环境变量打开：

    POWERNEWS_KEEP_RAW=true

## 目录结构

    data/raw/<source_id>/<YYYY-MM-DD>/<url_hash>.html.gz

按「站点 → 日期」两级，与 CSV 输出层（`data/csv/`）保持一致的检索习惯。
"""

from __future__ import annotations

import gzip
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from newsagg.export import safe_path_name as _safe
from newsagg.logutil import get_logger

log = get_logger("rawstore")

__all__ = ["RawRecord", "RawStore", "iter_raw", "raw_store_for"]

#: 单条原始响应落盘前的压缩阈值。小于这个大小也压缩 ——
#: gzip 对 HTML 的压缩比通常 5~10 倍，小文件压缩收益依然为正。
_GZIP_LEVEL = 6

# ⚠️ 路径清洗**直接复用** `export.py::safe_path_name`，不要在这里再写一份。
#
# 曾经复制了一份并手写成 `set(cleaned) > {"_"}`（**真超集**）——
# 语义反了：任何**不含下划线**的名字（如 40 位 hex 的 url_hash）都不满足
# `⊇ {"_"}`，于是全部走 fallback，文件名叫成了 `nohash-<时间戳>.html.gz`。
# 而 `gov_gz_nyj` 这类**含下划线**的名字恰好通过，所以目录名看着正常 ——
# 掩盖了 bug，只在文件级暴露。
#
# 教训：**同一套路径清洗只该有一份实现**。复制 = 迟早分叉。


@dataclass(frozen=True, slots=True)
class RawRecord:
    """一条落盘的原始响应。"""

    source_id: str
    day: str
    url_hash: str
    path: Path

    @property
    def url(self) -> str:
        """从同目录的 ``.url`` 侧车文件读回原始 URL（用于重洗时定位）。"""
        sidecar = self.path.with_suffix(".url")
        if sidecar.exists():
            return sidecar.read_text(encoding="utf-8").strip()
        return ""


class RawStore:
    """原始响应存储。

    Args:
        base_dir: 根目录（默认 ``<项目根>/data/raw``）。
        source_id: 来源标识（用于一级目录）。
        enabled: 是否启用落盘。关时所有方法都是**廉价空操作**。
    """

    def __init__(self, base_dir: str | Path, *, source_id: str, enabled: bool = False) -> None:
        self.base_dir = Path(base_dir)
        self.source_id = _safe(source_id, fallback="unknown")
        self.enabled = enabled
        self._written = 0

    # ---------------------------------------------------------------- 写
    def save(
        self,
        url: str,
        url_hash: str,
        body: bytes,
        *,
        status: int = 200,
        when: datetime | None = None,
    ) -> Path | None:
        """落盘一份原始响应（gzip 压缩）。

        Args:
            url: 原始 URL（写入 ``.url`` 侧车文件，重洗时靠它定位）。
            url_hash: URL 指纹（文件名）。
            body: 响应正文（bytes，**未解码** —— 原始字节才能保证编码可追溯）。
            status: HTTP 状态码，非 200 也存（失败样本同样有价值）。
            when: 采集时间，默认当前。

        Returns:
            落盘路径；未启用或写入失败时返回 ``None``。

        Note:
            **吞掉自己的异常**：原始层是"后悔药"，它的失败不该让采集失败。
            入库是主链路，原始层是附加保障 —— 这一点必须贯彻。
        """
        if not self.enabled:
            return None

        try:
            ts = when or datetime.now()
            day = ts.strftime("%Y-%m-%d")
            out_dir = self.base_dir / self.source_id / day
            out_dir.mkdir(parents=True, exist_ok=True)

            name = _safe(url_hash, fallback="nohash")
            path = out_dir / f"{name}.html.gz"

            # 同名不覆盖：同一 URL 在不同时间抓到的内容可能不同
            # （站点更新了稿件），覆盖会丢掉旧版本这个"快照"价值。
            if path.exists():
                path = out_dir / f"{name}-{ts.strftime('%H%M%S%f')[:9]}.html.gz"

            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                with gzip.open(tmp, "wb", compresslevel=_GZIP_LEVEL) as fh:
                    fh.write(body)
                tmp.replace(path)                 # 原子替换，不留半截文件
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

            # 侧车文件记 URL 与状态码：正文里认不出原始地址，
            # 重洗时必须知道"这份 HTML 对应哪个页面"
            path.with_suffix(".url").write_text(url, encoding="utf-8")
            path.with_suffix(".meta").write_text(
                f"status={status}\ncollected_at={ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]}\n",
                encoding="utf-8",
            )
            self._written += 1
            return path
        except Exception as exc:  # noqa: BLE001 - 原始层失败不影响采集
            log.warning("原始响应落盘失败（采集不受影响）：%s: %s", type(exc).__name__, exc)
            return None

    # ---------------------------------------------------------------- 读
    def load(self, path: str | Path) -> bytes:
        """读回一份原始响应（解压）。

        Raises:
            OSError: 文件不存在或不是合法 gzip。
        """
        with gzip.open(Path(path), "rb") as fh:
            return fh.read()

    @property
    def written(self) -> int:
        """本次会话已落盘条数。"""
        return self._written


def iter_raw(
    base_dir: str | Path,
    *,
    source_id: str | None = None,
    day: str | None = None,
) -> Iterator[RawRecord]:
    """遍历已落盘的原始响应，供重洗工具消费。

    Args:
        base_dir: 根目录。
        source_id: 只遍历该站点（``None`` = 全部）。
        day: 只遍历该日期（``YYYY-MM-DD``，``None`` = 全部）。

    Yields:
        :class:`RawRecord`，按「站点 → 日期 → 文件名」排序（**顺序稳定**，
        便于重洗时复现同样的处理次序）。
    """
    base = Path(base_dir)
    if not base.exists():
        return

    sources = [base / _safe(source_id)] if source_id else sorted(
        p for p in base.iterdir() if p.is_dir()
    )
    for src_dir in sources:
        if not src_dir.is_dir():
            continue
        days = [src_dir / day] if day else sorted(p for p in src_dir.iterdir() if p.is_dir())
        for day_dir in days:
            if not day_dir.is_dir():
                continue
            for path in sorted(day_dir.glob("*.html.gz")):
                yield RawRecord(
                    source_id=src_dir.name,
                    day=day_dir.name,
                    url_hash=path.name[: -len(".html.gz")],
                    path=path,
                )


def raw_store_for(
    source_id: str,
    *,
    base_dir: str | Path | None = None,
    enabled: bool | None = None,
) -> RawStore:
    """按来源构造 :class:`RawStore`。

    Args:
        source_id: 来源标识。
        base_dir: 根目录，默认 ``<项目根>/data/raw``。
        enabled: 是否启用；``None`` 时读环境变量 ``POWERNEWS_KEEP_RAW``
            （**默认关闭** —— 长期运行且无人归档时磁盘会无声涨满）。

    Returns:
        配置好的存储实例。
    """
    base = Path(base_dir) if base_dir else (
        Path(__file__).resolve().parents[1] / "data" / "raw"
    )
    if enabled is None:
        enabled = (os.getenv("POWERNEWS_KEEP_RAW") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
    return RawStore(base, source_id=source_id, enabled=enabled)
