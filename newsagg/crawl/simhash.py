"""正文 SimHash 去重（改造方案 §3.4「增量采集与去重」）。

对正文分词后计算 64 位 SimHash，两篇文章的汉明距离小于等于阈值即判定为
转载/重复内容（电力新闻跨省转载非常常见：国网省级公司大量转载新华社、
人民日报、中国电力新闻网的通稿）。

实现要点：
- 不引入 jieba 等重依赖，用「字 + 二元组」切分，对中文新闻足够；
- 汉明距离用 ``(x ^ y).bit_count()``（Python 3.10+ 原生整数方法）。
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["simhash64", "hamming", "is_similar", "DEFAULT_THRESHOLD"]

#: 默认汉明距离阈值（≤3 判为近似重复）
DEFAULT_THRESHOLD = 3

_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5]|[A-Za-z0-9]+")
_WS_RE = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    """切分文本为 token：单字 + 相邻二元组（中文新闻的近似词粒度）。"""
    if not text:
        return []
    cleaned = _WS_RE.sub("", text)
    chars = _TOKEN_RE.findall(cleaned)
    if not chars:
        return []
    tokens = list(chars)
    # 这里 zip 的两侧长度**刻意**不相等：chars[1:] 比 chars 少一个元素，
    # 正是「相邻字两两组合」想要的语义。因此显式写 strict=False ——
    # 若写成 strict=True，每次调用都会直接抛 ValueError。
    tokens.extend(a + b for a, b in zip(chars, chars[1:], strict=False))
    return tokens


def _hash64(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")


def simhash64(text: str, *, max_tokens: int = 8000) -> int:
    """计算文本的 64 位 SimHash。空文本返回 0。"""
    tokens = _tokens(text)
    if not tokens:
        return 0
    if len(tokens) > max_tokens:
        step = len(tokens) // max_tokens + 1
        tokens = tokens[::step]

    weights = [0] * 64
    for token in tokens:
        h = _hash64(token)
        for bit in range(64):
            if h >> bit & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1

    fingerprint = 0
    for bit in range(64):
        if weights[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming(a: int, b: int) -> int:
    """两个 SimHash 的汉明距离。"""
    return (a ^ b).bit_count()


def is_similar(a: int, b: int, *, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """是否近似重复。任一为 0（空正文）时返回 False。"""
    if not a or not b:
        return False
    return hamming(a, b) <= threshold
