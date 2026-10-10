"""URL 布隆过滤器（Redis 位图）。

去重体系共三层，本模块是第 2 层：

1. **URL 指纹**：``urls.url_hash`` + 唯一键 ``uk_articles_source_hash``（库内强一致）；
2. **布隆过滤器**（本模块）：跨任务运行期的快速判重，避免每次详情请求都查库；
3. **正文 SimHash**：``crawl/simhash.py``，识别跨来源转载。

布隆位图按「日期分桶」复用 Redis 位图，默认 2^24 位（2MB/桶）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from newsagg.logutil import get_logger

log = get_logger("dedup")

__all__ = ["UrlBloom", "DEFAULT_BITS", "DEFAULT_HASHES"]

#: 位图位数（2^24 = 16,777,216 位 ≈ 2MB）
DEFAULT_BITS = 1 << 24
#: 哈希函数个数
DEFAULT_HASHES = 4
#: 键前缀与桶数（按天分桶，7 天自动轮转，避免无限增长）
_KEY_PREFIX = "powernews:bloom:url"
_BUCKETS = 7


class UrlBloom:
    """基于 Redis ``SETBIT`` / ``GETBIT`` 的布隆过滤器。"""

    def __init__(self, client, *, bits: int = DEFAULT_BITS,
                 hashes: int = DEFAULT_HASHES, prefix: str = _KEY_PREFIX) -> None:
        self.client = client
        self.bits = bits
        self.hashes = hashes
        self.prefix = prefix

    # ---------------- 键与位 ----------------
    def _key(self, bucket: int | None = None) -> str:
        index = bucket if bucket is not None else datetime.now().timetuple().tm_yday % _BUCKETS
        return f"{self.prefix}:{index}"

    def _offsets(self, value: str) -> list[int]:
        """双重哈希派生出 ``hashes`` 个位偏移。

        ⚠️ 必须用**稳定哈希**（blake2b），不能用内置 ``hash()``：
        CPython 对 str 的 ``hash()`` 默认按进程随机加盐（PYTHONHASHSEED），
        而位图是多个 worker 进程共享的 Redis 位图——一旦各进程对同一 URL
        算出不同偏移，布隆过滤器就会在跨进程时退化成「永远 miss」，
        去重第二层直接失效（且不会报错，极难发现）。
        """
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1  # 保证为奇数，避免与 h1 线性相关
        return [((h1 + i * h2) % self.bits) for i in range(self.hashes)]

    # ---------------- 主接口 ----------------
    def add(self, value: str) -> None:
        """写入指纹。"""
        if not value:
            return
        offsets = self._offsets(value)
        key = self._key()
        pipe = self.client.pipeline()
        for offset in offsets:
            pipe.setbit(key, offset, 1)
        pipe.execute()

    def contains(self, value: str) -> bool:
        """判断指纹是否**可能**已存在（布隆过滤器存在假阳性）。"""
        if not value:
            return False
        offsets = self._offsets(value)
        key = self._key()
        pipe = self.client.pipeline()
        for offset in offsets:
            pipe.getbit(key, offset)
        return all(pipe.execute())

    def add_many(self, values: list[str]) -> int:
        """批量写入，返回写入条数。"""
        count = 0
        for value in values:
            self.add(value)
            count += 1
        return count

    def seen(self, value: str) -> bool:
        """:meth:`contains` 的语义化别名（供 session 使用）。"""
        return self.contains(value)

    def mark(self, value: str) -> None:
        """:meth:`add` 的语义化别名。"""
        self.add(value)

    # ---------------- 运维 ----------------
    def clear(self, bucket: int | None = None) -> None:
        """清空位图（单桶或全部）。"""
        if bucket is None:
            keys = [self._key(i) for i in range(_BUCKETS)]
        else:
            keys = [self._key(bucket)]
        for key in keys:
            try:
                self.client.delete(key)
            except Exception as exc:  # noqa: BLE001
                log.warning("清空布隆位图失败 %s: %s", key, exc)

    def fill_ratio(self, bucket: int | None = None) -> float:
        """估算位图填充率（>0.5 时假阳性显著上升，应清空）。"""
        key = self._key(bucket)
        try:
            return float(self.client.bitcount(key) or 0) / self.bits
        except Exception:  # noqa: BLE001
            return 0.0
