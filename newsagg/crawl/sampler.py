"""失败页采样落盘。

抓取失败时把响应体留一份到 ``samples/``，便于事后离线分析
（站点改版、反爬加严、路径失效）。文件名带 label + 状态码 + 时间戳，
同名时追加序号避免覆盖。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from newsagg.logutil import get_logger

log = get_logger("sampler")

__all__ = ["Sampler"]

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str, limit: int = 60) -> str:
    return _SAFE_RE.sub("_", text)[:limit].strip("_") or "unnamed"


class Sampler:
    """失败样本采样器。"""

    def __init__(self, base_dir: str | Path, *, enabled: bool = True,
                 max_per_run: int = 200) -> None:
        self.base_dir = Path(base_dir)
        self.enabled = enabled
        self.max_per_run = max_per_run
        self._written = 0

    def save_failure(self, url: str, status: int, body: str | None, *,
                     label: str = "detail") -> Path | None:
        """保存一份失败样本，返回落盘路径（未保存返回 None）。"""
        if not self.enabled or self._written >= self.max_per_run:
            return None

        day_dir = self.base_dir / datetime.now().strftime("%Y%m%d")
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("采样目录创建失败: %s", exc)
            return None

        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        name = f"{_slug(label)}_{status}_{_slug(url.rsplit('/', 1)[-1])}_{digest}.html"
        path = day_dir / name

        header = (
            f"<!-- powernews failure sample\n"
            f"     url   : {url}\n"
            f"     status: {status}\n"
            f"     at    : {datetime.now().isoformat(timespec='seconds')}\n"
            f"-->\n"
        )
        try:
            path.write_text(header + (body or ""), encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("样本写盘失败 %s: %s", path, exc)
            return None

        self._written += 1
        log.info("已保存失败样本: %s", path)
        return path

    @property
    def written(self) -> int:
        """本次运行已保存的样本数。"""
        return self._written
