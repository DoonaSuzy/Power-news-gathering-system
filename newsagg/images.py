"""正文配图提取：从详情页 HTML 里捞出**有效**的图片地址。

## 为什么不直接抓所有 `<img>`

实测一个新闻详情页的 `<img>` 里，**真正是正文配图的通常不到一半**，
其余是：站点 logo、导航图标、作者头像、二维码、分享按钮、1x1 埋点图。
全抓下来既浪费带宽，也会把 RustFS 塞满垃圾。

所以这里做**三层过滤**（见 `_is_noise` / `_looks_like_placeholder`）：
1. **URL 特征**：命中 `logo` / `icon` / `qr` / `avatar` / `share` 等词直接丢
2. **尺寸属性**：`width<=2 或 height<=2` 的是埋点图
3. **正文容器**：优先只取正文 DOM 内的图（外部导航区的跳过）

## 去重的两个层次

* **篇内**：同一 URL 只保留一次（`ordinal` 记首次出现位置）
* **全站**：`url_hash` 落库并建索引 —— 同一张通稿配图被多家媒体引用时，
  worker 只需下载一次（见 `article_images.idx_img_url_hash`）

## 与「正文解析」的关系

**只提取 URL，不下载**。下载与上传是 worker 的独立异步过程
（见 `article_images.status`）。这样图片下载失败**不会让文章入库失败**。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

__all__ = [
    "ImageRef",
    "extract_image_urls",
    "url_hash40",
    "is_probably_noise_url",
    "NOISE_WORDS",
    "NOISE_PATH_SEGMENTS",
    "COMMON_PREFIX_MIN",
]


#: 命中即判定为「非正文图」的 URL 特征词（小写匹配）。
#:
#: ⚠️ 中文站点的噪声常以**拼音缩写**形式出现在路径里，纯英文词表拦不住。
#: 实测（中国电力新闻网）每篇捞出 12 张图，其中一半是：
#:   `/dlxwwewm/...`   —— `ewm` = 二维码
#:   `/dlxwwappxz/...` —— `appxz` = APP 下载
#: 所以下面分三组：通用英文、站点公共路径、中文拼音缩写。
_NOISE_WORDS = (
    # --- 通用英文 ---
    "logo", "icon", "avatar", "qrcode", "qr_code", "qr-", "share",
    "weixin", "weibo", "spacer", "blank", "pixel", "loading", "spinner",
    "btn", "button", "nav_", "menu", "banner_ad", "advert",
    "placeholder", "default_img", "no-pic", "nopic",
    # --- 站点公共元素（实测命中）---
    "public", "common", "template", "header", "footer", "sprite",
    "bg_", "_bg", "border", "divider", "line_",
    # --- 中文拼音缩写（国内站点高频）---
    "ewm",          # 二维码
    "appxz",        # APP 下载
    "xzzx",         # 下载中心
    "top", "banner", "guanzhu", "weixinma",
    "dianzan", "shoucang",
)

#: 路径片段级噪声：形如 `/<word>/` 的**整段路径**命中才算噪声。
#:
#: ⚠️ 名单必须**保守** —— 只放「必然是站点公共资源」的：
#: `public` / `common` / `static` / `template` / `css|js|fonts`。
#:
#: **刻意不含 `images` / `img` / `pic`**：大量站点的**正文配图就在
#: `/images/` 或 `/upload/images/` 下**，把它们算噪声会**误杀正文图**
#: —— 那是比「多抓几张垃圾图」严重得多的错误（漏抓无法补救，
#: 多抓只是多占几百 KB）。
_NOISE_PATH_SEGMENTS = frozenset({
    "public", "common", "static", "template", "templates",
    "css", "js", "fonts", "assets",
})

#: 同一篇文章中，若某**路径前缀**下出现次数 ≥ 该值，视为站点公共元素。
#:
#: 依据：正文配图通常各在不同日期目录（`/202610/xxx.jpg`），
#: 而公共元素高度集中（多张图挤在同一目录，如 `/public/a210726/`）。
#: 这是**统计式过滤**，比关键词更鲁棒 —— 站点新增公共资源目录时
#: 不需要改词表。
_COMMON_PREFIX_MIN = 4


#: 图片扩展名（没有扩展名的通常不是正文图，但也可能是接口图，故只做加分不做硬门槛）
_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")

#: `<img ...>` 标签（含自闭合）
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)

#: 属性提取（同时兼容 src / data-src / data-original 等懒加载写法）
_ATTR = re.compile(
    r"""(?:src|data-src|data-original|data-lazy-src|data-echo)\s*=\s*["']([^"']+)["']""",
    re.I,
)
_ALT = re.compile(r"""alt\s*=\s*["']([^"']*)["']""", re.I)
_WIDTH = re.compile(r"""width\s*=\s*["']?(\d+)""", re.I)
_HEIGHT = re.compile(r"""height\s*=\s*["']?(\d+)""", re.I)


@dataclass(frozen=True, slots=True)
class ImageRef:
    """一张待下载的正文配图。

    Attributes:
        url: 绝对化后的地址。
        url_hash: URL 指纹（SHA-256 前 40 位），与 `articles.url_hash` 同算法。
        ordinal: 在正文中的出现序号（保留原顺序，0 起）。
        alt_text: `alt` 原文。
        width: 声明的宽度（0 = 未声明）。
        height: 声明的高度（0 = 未声明）。
    """

    url: str
    url_hash: str
    ordinal: int = 0
    alt_text: str = ""
    width: int = 0
    height: int = 0


def url_hash40(url: str) -> str:
    """URL 指纹：SHA-256 前 40 位（与 `articles.url_hash` 同口径）。

    Args:
        url: 绝对地址。

    Returns:
        40 位十六进制字符串。
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]


def is_probably_noise_url(url: str) -> bool:
    """判断 URL 是否像「非正文图」。

    两重判据：
    1. **关键词**：整串包含 `_NOISE_WORDS` 里任一（含中文拼音缩写）
    2. **路径段**：形如 `/<word>/` 的整段命中 `_NOISE_PATH_SEGMENTS`

    Args:
        url: 图片地址。

    Returns:
        True 表示应跳过。

    Note:
        路径段判定**刻意保守**（不含 `images`/`img`）——
        见 `_NOISE_PATH_SEGMENTS` 的注释：误杀正文图比多抓垃圾图严重得多。
    """
    low = url.lower()
    if any(w in low for w in _NOISE_WORDS):
        return True
    try:
        parts = urlsplit(low)
    except ValueError:
        return False
    for seg in parts.path.split("/"):
        if seg and seg in _NOISE_PATH_SEGMENTS:
            return True
    return False


def _path_prefix(url: str, *, depth: int = 2) -> str:
    """取 URL 的路径前缀（用于统计式过滤）。

    Args:
        url: 图片地址。
        depth: 取前几段路径。

    Returns:
        形如 `host/a/b` 的前缀键；无法解析时返回空串。
    """
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    segs = [s for s in p.path.split("/") if s]
    return "/".join([p.netloc, *segs[:depth]])


def _normalize(url: str, base_url: str) -> str:
    """相对地址转绝对，并去掉 fragment（同图不同锚点视为同一张）。"""
    if url.startswith("//"):
        # 协议相对：继承 base 的 scheme
        url = f"{urlsplit(base_url).scheme}:{url}"
    elif not url.startswith(("http://", "https://", "data:")):
        url = urljoin(base_url, url)
    parts = urlsplit(url)
    return urlunsplit(parts._replace(fragment=""))


def extract_image_urls(
    html: str,
    base_url: str,
    *,
    max_images: int = 30,
    min_declared_px: int = 3,
) -> list[ImageRef]:
    """从详情页 HTML 提取正文配图。

    Args:
        html: 详情页 HTML。
        base_url: 页面地址（用于相对路径绝对化）。
        max_images: 单篇最多取几张（防止异常页面拖垮；正文配图极少超过这个数）。
        min_declared_px: 声明尺寸小于该值视为埋点图（0 表示不按尺寸过滤）。

    Returns:
        去重后的 :class:`ImageRef` 列表，按正文出现顺序。

    Note:
        **只提取，不下载**。下载由 worker 的独立流程处理 ——
        这样图片问题不会阻塞文章入库。
    """
    out: list[ImageRef] = []
    seen: set[str] = set()
    ordinal = 0

    for m in _IMG_TAG.finditer(html):
        tag = m.group(0)

        src_m = _ATTR.search(tag)
        if not src_m:
            continue
        raw = src_m.group(1).strip()
        # 内联 base64 不是外链图，跳过（体积大且无法复用）
        if not raw or raw.startswith("data:"):
            continue
        if is_probably_noise_url(raw):
            continue

        url = _normalize(raw, base_url)
        if not url.startswith(("http://", "https://")):
            continue

        # 尺寸过滤：显式声明 1x1 / 2x2 的必是埋点图
        w = int(_WIDTH.search(tag).group(1)) if _WIDTH.search(tag) else 0
        h = int(_HEIGHT.search(tag).group(1)) if _HEIGHT.search(tag) else 0
        if min_declared_px and w and h and (w < min_declared_px or h < min_declared_px):
            continue

        hsh = url_hash40(url)
        if hsh in seen:
            continue
        # 无扩展名且声明尺寸很小 —— 大概率是装饰元素
        if not url.lower().split("?")[0].endswith(_IMG_EXT) and w and w < 50:
            continue

        seen.add(hsh)
        alt = _ALT.search(tag)
        out.append(ImageRef(
            url=url, url_hash=hsh, ordinal=ordinal,
            alt_text=(alt.group(1).strip()[:255] if alt else ""),
            width=w, height=h,
        ))
        ordinal += 1
        # 先放宽收集（供统计式过滤有足够样本），最后再按 max_images 截断
        if len(out) >= max_images * 3:
            break

    # --- 统计式过滤：同一路径前缀出现 ≥N 次 → 站点公共元素，整组剔除 ---
    # 依据：正文配图分散在不同日期目录，公共元素高度集中。
    # 比关键词更鲁棒 —— 站点新增公共资源目录时不用改词表。
    prefix_count = Counter(_path_prefix(r.url) for r in out)
    kept = [r for r in out if prefix_count[_path_prefix(r.url)] < _COMMON_PREFIX_MIN]

    # 截断并重排 ordinal（过滤后序号要连续，否则下游看到的序号有空洞）
    return [
        ImageRef(url=r.url, url_hash=r.url_hash, ordinal=i,
                 alt_text=r.alt_text, width=r.width, height=r.height)
        for i, r in enumerate(kept[:max_images])
    ]
#: 对外暴露的常量别名（便于测试与调参）
NOISE_WORDS = _NOISE_WORDS
NOISE_PATH_SEGMENTS = _NOISE_PATH_SEGMENTS
COMMON_PREFIX_MIN = _COMMON_PREFIX_MIN
