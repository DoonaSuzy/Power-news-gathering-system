"""URL 规范化与稳定哈希。

去重体系的第一层：同一篇文章在不同页面出现时（带 utm/spm/from/scene 追踪参数、
http/https 混用、大小写主机、末尾斜杠、片段锚点），必须归一到同一个指纹。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["normalize_url", "url_hash", "host_of", "same_site", "is_absolute"]

#: 需要剔除的追踪参数（前缀匹配）
_TRACKING_PREFIXES = ("utm_", "spm", "from", "scene", "share", "wx_", "tt_", "weibo_")
_TRACKING_EXACT = {
    "from", "scene", "share_token", "share_source", "spm", "spmid",
    "ref", "referer", "referrer", "source_url", "timestamp",
}

_WS_RE = re.compile(r"\s+")


def normalize_url(url: str) -> str:
    """规范化 URL，用于生成去重指纹。

    规则：小写 scheme/host、去 www 前缀差异不处理（保留原主机语义）、
    去默认端口、去片段、剔除追踪参数、路径保留大小写（部分 CMS 大小写敏感）。
    """
    if not url:
        return ""
    url = _WS_RE.sub("", url.strip())

    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return url

    host = (parts.hostname or "").lower()
    if not host:
        return url
    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"

    path = parts.path or "/"

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if low in _TRACKING_EXACT or low.startswith(_TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """URL → 稳定哈希（SHA-256 前 40 位十六进制）。

    唯一键 ``uk_articles_source_hash(source_id, url_hash)`` 使用本值。
    """
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:40]


def host_of(url: str) -> str:
    """取主机名（小写）。"""
    return (urlsplit(url).hostname or "").lower()


def same_site(url_a: str, url_b: str) -> bool:
    """判断两个 URL 是否同站（按注册域近似：取主机名末两段）。"""
    a, b = host_of(url_a), host_of(url_b)
    if not a or not b:
        return False
    return a.split(".")[-2:] == b.split(".")[-2:]


def is_absolute(url: str) -> bool:
    """是否为绝对 URL。"""
    return urlsplit(url).scheme in ("http", "https") and bool(urlsplit(url).netloc)
