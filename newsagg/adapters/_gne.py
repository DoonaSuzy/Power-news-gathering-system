"""GNE 通用正文抽取 fallback（改造方案 §3.2 / §4.1）。

GNE（General News Extractor）基于文本与符号密度算法，在中国主流新闻站点上
有很高准确率，支持自动检测标题、发布时间、作者。在本项目里它只承担
**兜底**角色：专用适配器解析不出正文时才调用，以控制性能开销。

设计要点：

- **惰性导入**：GNE 未安装或未启用时不引入任何开销；
- **失败静默**：GNE 抛异常时返回 ``None``，绝不影响主流程；
- **可观测**：返回结果的 ``used_fallback`` 由适配器写入 ``DetailParse``，
  便于统计「哪些来源还在依赖兜底」。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from newsagg.logutil import get_logger

log = get_logger("gne")

__all__ = ["gne_available", "extract_with_gne", "reset_gne_cache"]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@lru_cache(maxsize=1)
def gne_available() -> bool:
    """GNE 是否可用（已安装且未被 ``POWERNEWS_GNE_FALLBACK=false`` 关闭）。"""
    if os.environ.get("POWERNEWS_GNE_FALLBACK", "true").strip().lower() in {
        "0", "false", "no", "off"
    }:
        return False
    try:
        import gne  # noqa: F401
    except ImportError:
        log.info("GNE 未安装，通用正文抽取兜底已停用（pip install gne 可启用）")
        return False
    return True


def _split_paragraphs(text: str) -> list[str]:
    """把 GNE 给出的整段正文切成段落列表。"""
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    if len(parts) <= 1:
        # 无换行时按中文句末切分，保证段落粒度与专用适配器一致
        parts = [
            seg.strip()
            for seg in re.split(r"(?<=[。！？；])", text.replace("\u3000", " "))
            if seg.strip()
        ]
    return [p for p in parts if len(p) >= 8]


def extract_with_gne(html: str, *, url: str = "") -> dict | None:
    """用 GNE 抽取正文。失败或不可用时返回 ``None``。

    返回 ``{"title", "paragraphs", "published_text", "author"}``。
    """
    if not html or not gne_available():
        return None

    try:
        from gne import GeneralNewsExtractor

        result = GeneralNewsExtractor().extract(
            html, title_xpath="", author_xpath="", publish_time_xpath=""
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("GNE 抽取失败 %s: %s", url, exc)
        return None

    if not isinstance(result, dict):
        return None

    paragraphs = _split_paragraphs(result.get("content") or "")
    title = (result.get("title") or "").strip()
    published = (result.get("publish_time") or "").strip()

    if not paragraphs and not title:
        return None

    if published and len(published) < 8:
        # GNE 偶尔只给出短串，补齐成可被 parse_published 识别的形态
        published = published.strip()

    return {
        "title": title,
        "paragraphs": paragraphs,
        "published_text": published,
        "author": (result.get("author") or "").strip(),
    }


def reset_gne_cache() -> None:
    """清空可用性缓存（测试用）。"""
    gne_available.cache_clear()


def clean_for_gne(html: str, *, limit: int = 400_000) -> str:
    """GNE 前的轻量预处理（当前仅做长度保护）。"""
    if len(html) <= limit:
        return html
    return html[:limit]
