"""数据清洗：标题 / 正文 / 来源 / 时间的规范化。

五类字段（标题、正文纯文本、发布时间、原文来源、原文链接）中，除链接外
全部经本模块处理后才落库。这里的规则来自新闻站点真实样本的归纳，
新增规则请附带测试用例。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "PRECISION_SECOND",
    "PRECISION_MINUTE",
    "PRECISION_DATE",
    "PRECISION_UNKNOWN",
    "ParsedTime",
    "clean_title",
    "clean_text",
    "clean_paragraphs",
    "clean_source",
    "extract_source",
    "looks_like_source",
    "parse_published",
    "to_cst_naive",
    "to_utc_naive",
    "content_hash",
    "quality_flags",
    "csv_cell",
]

PRECISION_SECOND = "second"
PRECISION_MINUTE = "minute"
PRECISION_DATE = "date"
PRECISION_UNKNOWN = "unknown"

#: 北京时间固定 UTC+8（自 1991 年起无夏令时）
CST = timezone(timedelta(hours=8))

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\s\u3000]+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\xa0]")

#: 标题里需要剔除的噪声后缀
_TITLE_NOISE = re.compile(
    r"(\s*[-_|—–]\s*(?:搜狐|新浪|网易|腾讯|凤凰网|今日头条|人民网|新华网|央视网|"
    r"中国电力新闻网|北极星电力网|国际能源网)[^\s]*\s*$)",
    re.I,
)

#: 来源名里常见的时间/编辑污染
_SOURCE_POLLUTION = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?.*$|来源\s*[：:]|"
    r"责任编辑\s*[：:].*$|作者\s*[：:].*$|编辑\s*[：:].*$|"
    r"\d{1,2}:\d{2}(:\d{2})?.*$)",
)

#: 像媒体名的特征词
_SOURCE_HINT = re.compile(
    r"(日报|晚报|商报|晨报|时报|周报|党报|新闻网|电视台|广播|融媒体|"
    r"通讯社|电力|电网|能源|集团|公司|客户端|新华|人民|央视|中国)",
)

_QUALITY_NOISE = re.compile(
    r"(版权所有|Copyright|ICP|备案号|主办单位|承办单位|技术支持|友情链接|"
    r"扫一扫|分享到|上一条|下一条|阅读次数|更多资讯|下载客户端)",
    re.I,
)


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------
def clean_text(raw: str) -> str:
    """剥标签 + 压缩空白 + 去零宽字符。"""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = _ZERO_WIDTH_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def clean_title(raw: str) -> str:
    """清洗标题：去标签、去站点名后缀、限长。"""
    text = clean_text(raw)
    if not text:
        return ""
    text = _TITLE_NOISE.sub("", text).strip(" -_|—–\u3000")
    return text[:300]


def clean_paragraphs(paragraphs: list[str] | None) -> list[str]:
    """清洗正文段落：去空、去噪、去重（保序）。"""
    if not paragraphs:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for para in paragraphs:
        text = clean_text(para)
        if len(text) < 8:
            continue
        if _QUALITY_NOISE.search(text) and len(text) < 80:
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def csv_cell(value: object, limit: int = 32000) -> str:
    """CSV 单元格安全化：去换行、截断。"""
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return text[:limit]


# --------------------------------------------------------------------------
# 来源
# --------------------------------------------------------------------------
def extract_source(raw: str) -> str:
    """从原始文本中剥离时间/编辑/「来源：」等污染，抽出真正的来源名。"""
    text = clean_text(raw)
    if not text:
        return ""
    text = _SOURCE_POLLUTION.sub("", text).strip(" 　:：|·-")
    return text[:64]


def looks_like_source(text: str) -> bool:
    """判断一个字符串像不像媒体名（用于决定是否采纳）。"""
    if not text or len(text) > 32:
        return False
    if re.fullmatch(r"[\d\s\-/:：.年月日]+", text):
        return False
    return bool(_SOURCE_HINT.search(text))


def clean_source(raw: str) -> str:
    """清洗来源名；不像媒体名时仅在长度合规时保留。"""
    cleaned = extract_source(raw)
    if not cleaned:
        return ""
    if looks_like_source(cleaned):
        return cleaned
    return cleaned if len(cleaned) <= 20 else ""


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ParsedTime:
    """时间解析结果。``value`` 为北京时间 naive datetime。"""

    value: datetime | None
    precision: str = PRECISION_UNKNOWN


#: (正则, 字段位, 精度) —— 顺序即优先级，先匹配到的精度最高
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})[ T]"
                r"(?P<H>\d{1,2}):(?P<M>\d{2}):(?P<S>\d{2})"), "y m d H M S", PRECISION_SECOND),
    (re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})[ T]"
                r"(?P<H>\d{1,2}):(?P<M>\d{2})(?!:)"), "y m d H M", PRECISION_MINUTE),
    (re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})"), "y m d", PRECISION_DATE),
    (re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日\s*"
                r"(?P<H>\d{1,2}):(?P<M>\d{2})"), "y m d H M", PRECISION_MINUTE),
    (re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日"), "y m d", PRECISION_DATE),
    (re.compile(r"(?P<m>\d{1,2})-(?P<d>\d{1,2})\s+(?P<H>\d{1,2}):(?P<M>\d{2})"),
     "m d H M", PRECISION_MINUTE),
]

#: 可信发布时间下限（低于此值视为页脚年份污染）
MIN_TRUSTED_YEAR = 2000


def parse_published(raw: str | None, *, default_year: int | None = None) -> ParsedTime:
    """从原始文本解析发布时间（按北京时间处理，返回 naive datetime）。"""
    if not raw:
        return ParsedTime(None, PRECISION_UNKNOWN)

    text = clean_text(raw)
    year_default = default_year or datetime.now(CST).year

    for pattern, fields, precision in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        gd = match.groupdict()
        try:
            year = int(gd["y"]) if "y" in fields and gd.get("y") else year_default
            month = int(gd["m"])
            day = int(gd["d"])
            hour = int(gd["H"]) if "H" in fields and gd.get("H") else 0
            minute = int(gd["M"]) if "M" in fields and gd.get("M") else 0
            second = int(gd["S"]) if "S" in fields and gd.get("S") else 0
            value = datetime(year, month, day, hour, minute, second)
        except (TypeError, ValueError):
            continue
        if value.year < MIN_TRUSTED_YEAR:
            # 页脚版权年份被误认，按未知处理而非写入错误时间
            return ParsedTime(None, PRECISION_UNKNOWN)
        return ParsedTime(value, precision)

    return ParsedTime(None, PRECISION_UNKNOWN)


def to_utc_naive(cst_naive: datetime) -> datetime:
    """北京时间 naive → UTC naive。"""
    return (cst_naive.replace(tzinfo=CST)).astimezone(timezone.utc).replace(tzinfo=None)


def to_cst_naive(utc_naive: datetime) -> datetime:
    """UTC naive → 北京时间 naive。"""
    return (utc_naive.replace(tzinfo=timezone.utc)).astimezone(CST).replace(tzinfo=None)


def now_cst() -> datetime:
    """当前北京时间（naive）。"""
    return datetime.now(CST).replace(tzinfo=None)


def now_utc_naive() -> datetime:
    """当前 UTC 时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 指纹与质量标记
# --------------------------------------------------------------------------
def content_hash(paragraphs: list[str] | None) -> str:
    """正文指纹（SHA-256 前 40 位）。用于跨来源同文去重。"""
    if not paragraphs:
        return ""
    joined = "\n".join(clean_paragraphs(paragraphs))
    if not joined:
        return ""
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


def quality_flags(
    paragraphs: list[str] | None,
    *,
    is_media_only: bool = False,
    title: str = "",
    source_name: str = "",
    published: datetime | None = None,
) -> list[str]:
    """生成质量标记列表，便于事后筛选与统计。"""
    flags: list[str] = []
    text_len = sum(len(p) for p in (paragraphs or []))
    if text_len == 0:
        flags.append("empty_body")
    elif text_len < 120:
        flags.append("short_body")
    if is_media_only:
        flags.append("media_only")
    if not title:
        flags.append("no_title")
    if not source_name:
        flags.append("no_source")
    if published is None:
        flags.append("no_time")
    return flags
