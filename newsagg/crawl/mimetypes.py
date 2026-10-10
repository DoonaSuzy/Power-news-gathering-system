"""非文本页判定：把 PDF / 图片 / 视频 / 附件从详情抓取中剔除。

新闻站的列表页里常混着附件、视频播放页、图片专版，这些页面没有正文纯文本，
抓下来只会污染 ``empty_body`` 指标。本模块给出统一判定。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = ["is_non_text_url", "is_non_text_page", "NON_TEXT_SUFFIXES"]

#: 明确非文本的后缀
NON_TEXT_SUFFIXES: tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".mp3", ".wav", ".m4a",
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".m3u8", ".ts",
    ".apk", ".exe", ".dmg", ".woff", ".woff2", ".ttf",
)

#: 页面里的播放器/下载器特征
_PAGE_HINTS = re.compile(
    r"(<video[\s>]|<audio[\s>]|application/pdf|"
    r"\.(?:mp4|m3u8)\?|jwplayer|video-js|player-container)",
    re.I,
)

_VIDEO_HOST_HINT = re.compile(r"(^|\.)(v|video|tv|live)\.", re.I)


def is_non_text_url(url: str) -> bool:
    """按 URL 后缀判定是否非文本资源。"""
    path = urlsplit(url).path.lower()
    return path.endswith(NON_TEXT_SUFFIXES)


def is_non_text_page(url: str, html: str | None, *, min_chars: int = 200) -> bool:
    """判定响应是否为「非文本页」。

    三个判据任一命中即认为是非文本页：

    1. URL 后缀属于附件/媒体类型；
    2. 正文体量过小（``min_chars`` 以下）且页面含播放器特征；
    3. 页面几乎为空（剥离标签后不足 60 字）。
    """
    if is_non_text_url(url):
        return True

    if not html:
        return True

    stripped = re.sub(r"<[^>]+>", "", html)
    stripped = re.sub(r"\s+", "", stripped)

    if len(stripped) < 60:
        return True

    if len(html) < min_chars and _PAGE_HINTS.search(html):
        return True

    return False
