"""地区名 → 拼音 / 编码索引（基于 pypinyin）。

业务人员输入的地区写法千奇百怪：``广东``、``广东省``、``guangdong``、``GD``、
``粤``、``440000``……本模块用 pypinyin 把中文地区名转成拼音与首字母缩写，并建立
多键索引，使任意写法都能解析为统一的 :class:`RegionRecord`（中文名 + 6 位
行政区划码 + 拼音 + 缩写）。

索引构建是纯内存字典查找（O(1)），拼音转换只在构建时发生一次，避免在查询
热路径上重复调用 pypinyin。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pypinyin import Style, lazy_pinyin

from .errors import UnknownRegionError

__all__ = [
    "PROVINCES",
    "SINGLE_CHAR_ALIASES",
    "RegionRecord",
    "RegionIndex",
    "normalize_region",
    "to_pinyin",
    "to_pinyin_abbr",
    "region_pinyin",
    "region_code",
    "to_halfwidth",
    "build_region_index",
    "default_region_index",
]

#: (行政区划码, 地区名) 按 GB/T 2260 顺序，覆盖 34 个省级行政区
PROVINCES: tuple[tuple[str, str], ...] = (
    ("110000", "北京"),
    ("120000", "天津"),
    ("130000", "河北"),
    ("140000", "山西"),
    ("150000", "内蒙古"),
    ("210000", "辽宁"),
    ("220000", "吉林"),
    ("230000", "黑龙江"),
    ("310000", "上海"),
    ("320000", "江苏"),
    ("330000", "浙江"),
    ("340000", "安徽"),
    ("350000", "福建"),
    ("360000", "江西"),
    ("370000", "山东"),
    ("410000", "河南"),
    ("420000", "湖北"),
    ("430000", "湖南"),
    ("440000", "广东"),
    ("450000", "广西"),
    ("460000", "海南"),
    ("500000", "重庆"),
    ("510000", "四川"),
    ("520000", "贵州"),
    ("530000", "云南"),
    ("540000", "西藏"),
    ("610000", "陕西"),
    ("620000", "甘肃"),
    ("630000", "青海"),
    ("640000", "宁夏"),
    ("650000", "新疆"),
    ("710000", "台湾"),
    ("810000", "香港"),
    ("820000", "澳门"),
)

#: 单字简称（车牌/公文常用写法）→ 地区名
SINGLE_CHAR_ALIASES: dict[str, str] = {
    "京": "北京", "津": "天津", "冀": "河北", "晋": "山西", "蒙": "内蒙古",
    "辽": "辽宁", "吉": "吉林", "黑": "黑龙江", "沪": "上海", "苏": "江苏",
    "浙": "浙江", "皖": "安徽", "闽": "福建", "赣": "江西", "鲁": "山东",
    "豫": "河南", "鄂": "湖北", "湘": "湖南", "粤": "广东", "桂": "广西",
    "琼": "海南", "渝": "重庆", "川": "四川", "蜀": "四川", "贵": "贵州",
    "黔": "贵州", "云": "云南", "滇": "云南", "藏": "西藏", "陕": "陕西",
    "秦": "陕西", "甘": "甘肃", "陇": "甘肃", "青": "青海", "宁": "宁夏",
    "新": "新疆", "台": "台湾", "港": "香港", "澳": "澳门",
}

#: 需要从地区名尾部剥离的后缀（长后缀在前，避免「壮族自治区」被「自治区」抢先匹配）
_NAME_SUFFIXES: tuple[str, ...] = (
    "特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区",
    "自治区", "省", "市",
)

_FULLWIDTH = {ord("ａ") + i: ord("a") + i for i in range(26)}
_FULLWIDTH.update({ord("Ａ") + i: ord("A") + i for i in range(26)})
_FULLWIDTH.update({ord("０") + i: ord("0") + i for i in range(10)})
_FULLWIDTH.update({0xFF0D: ord("-"), 0x2013: ord("-"), 0x2014: ord("-"), 0x3000: ord(" ")})


def _clean(text: str) -> str:
    """全角转半角、压缩空白。"""
    return " ".join(to_halfwidth(text).split())


def to_halfwidth(text: str) -> str:
    """全角字符 → 半角（字母/数字/连接符/空白）。"""
    return str(text).translate(_FULLWIDTH)


def normalize_region(name: str | None) -> str:
    """归一化地区名：去首尾空白、全角转半角、剥离「省/市/自治区」等后缀。

    >>> normalize_region(" 广东省 ")
    '广东'
    >>> normalize_region("内蒙古自治区")
    '内蒙古'
    """
    if name is None:
        return ""
    text = _clean(str(name))
    if not text:
        return ""
    for suffix in _NAME_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip()


def _key(text: str) -> str:
    """索引键归一化：小写 + 去空白 + 去常见连接符。"""
    return "".join(ch for ch in _clean(text).lower() if ch.isalnum())


def to_pinyin(name: str, *, sep: str = "") -> str:
    """中文地区名 → 拼音（如 ``广东`` → ``guangdong``）。

    非中文输入原样返回，因此 ``to_pinyin("Guangdong")`` 同样得到 ``guangdong``。
    """
    text = normalize_region(name)
    if not text:
        return ""
    parts = lazy_pinyin(text, style=Style.NORMAL, errors=lambda item: [str(item)])
    return sep.join(str(p) for p in parts)


def to_pinyin_abbr(name: str) -> str:
    """中文地区名 → 拼音首字母缩写（如 ``广东`` → ``gd``）。"""
    text = normalize_region(name)
    if not text:
        return ""
    parts = lazy_pinyin(text, style=Style.FIRST_LETTER, errors=lambda item: [str(item)])
    return "".join(str(p)[0] for p in parts if str(p))


def region_pinyin(name: str) -> str:
    """模块级便捷函数：地区名 → 拼音。"""
    return to_pinyin(name)


def region_code(name: str) -> str | None:
    """地区名/代码 → 6 位行政区划码，无法识别返回 ``None``。"""
    text = _clean(str(name)) if name is not None else ""
    if not text:
        return None
    if text.isdigit() and len(text) == 6:
        return text
    record = default_region_index().lookup(name)
    return record.code if record else None


@dataclass(frozen=True)
class RegionRecord:
    """一个地区的标准化索引记录。"""

    code: str
    name: str
    pinyin: str
    abbr: str
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "pinyin": self.pinyin,
            "abbr": self.abbr,
            "aliases": list(self.aliases),
        }


@dataclass
class RegionIndex:
    """地区多键索引：中文名 / 拼音 / 缩写 / 单字简称 / 行政区划码 均可查。"""

    records: tuple[RegionRecord, ...] = ()
    _index: dict[str, RegionRecord] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._build_index()

    def _build_index(self) -> None:
        index: dict[str, RegionRecord] = {}
        for record in self.records:
            for key in self._keys(record):
                index.setdefault(key, record)
        self._index = index

    @staticmethod
    def _keys(record: RegionRecord) -> Iterable[str]:
        yield _key(record.name)
        yield _key(record.pinyin)
        yield _key(record.abbr)
        yield _key(record.code)
        for alias in record.aliases:
            yield _key(alias)

    # ------------------------------------------------------------------ 查询
    def lookup(self, query: str | None) -> RegionRecord | None:
        """按任意写法查找地区；找不到返回 ``None``。"""
        if query is None:
            return None
        text = _clean(str(query))
        if not text:
            return None
        record = self._index.get(_key(text))
        if record is not None:
            return record
        record = self._index.get(_key(normalize_region(text)))
        if record is not None:
            return record
        # 最后尝试把输入当拼音解析（如 "guang dong"）
        return self._index.get(_key(to_pinyin(text)))

    def resolve(self, query: str) -> RegionRecord:
        """同 :meth:`lookup`，但找不到时抛出 :class:`UnknownRegionError`。"""
        record = self.lookup(query)
        if record is None:
            raise UnknownRegionError(query, [r.name for r in self.records])
        return record

    def resolve_many(self, queries: Iterable[str] | str | None) -> tuple[RegionRecord, ...] | None:
        """解析地区列表；``None`` 表示「不限地区」。"""
        if queries is None:
            return None
        if isinstance(queries, str):
            queries = [queries]
        return tuple(self.resolve(item) for item in queries)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.records)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __contains__(self, query: object) -> bool:
        return isinstance(query, str) and self.lookup(query) is not None

    def to_frame(self):
        """导出为 DataFrame（便于人工核对索引）。"""
        import pandas as pd

        return pd.DataFrame([r.as_dict() for r in self.records])


def build_region_index(
    provinces: Iterable[tuple[str, str]] = PROVINCES,
    aliases: Mapping[str, str] | None = None,
) -> RegionIndex:
    """构建地区索引。

    :param provinces: ``(行政区划码, 地区名)`` 序列
    :param aliases: 额外别名映射（键为任意写法，值为标准地区名）
    """
    alias_map: dict[str, list[str]] = {}
    for alias, target in (aliases or {}).items():
        alias_map.setdefault(target, []).append(alias)
    for single, target in SINGLE_CHAR_ALIASES.items():
        alias_map.setdefault(target, []).append(single)

    records: list[RegionRecord] = []
    for code, name in provinces:
        records.append(
            RegionRecord(
                code=code,
                name=name,
                pinyin=to_pinyin(name),
                abbr=to_pinyin_abbr(name),
                aliases=tuple(sorted(set(alias_map.get(name, ())))),
            )
        )
    return RegionIndex(tuple(records))


_default_index: RegionIndex | None = None


def default_region_index() -> RegionIndex:
    """进程级默认地区索引（惰性构建）。"""
    global _default_index
    if _default_index is None:
        _default_index = build_region_index()
    return _default_index
