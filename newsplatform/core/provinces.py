"""省份「名称 → 行政区划代码」解析。

为什么要有这个模块
------------------
按省份筛选的接口原先只收 6 位代码（``?province=440000``），传「广东」直接 400。
这是照着 DDL 的 ``char(6)`` 写接口写出来的后果：**存储用代码是对的，
但接口面不该逼用户去记代码**。更糟的是 ``/sources?province=`` 那处压根没校验，
传「广东」会**静默返回 0 条**——比报错更难发现。

设计原则：宽松地收，严格地判
----------------------------
收的时候尽量容错，判的时候必须给出确定答案：

- ``440000`` / ``广东省`` / ``广东`` / ``华南`` / ``全国`` 都能收；
- 允许省略或补上后缀（省 / 市 / 自治区 / 特别行政区）；
- 忽略首尾空白与内部空白（``广 东`` 也能收）；
- ASCII 不区分大小写（``all`` / ``ALL``）；
- 真的解析不出来时抛 :class:`BadRequest`，并在消息里**列出所有可用取值**——
  用户不必再去翻文档。

为什么「大区」也能收
--------------------
``provinces.region`` 是现成的分组（华北 / 东北 / 华东 / 华中 / 华南 / 西南 / 西北 / 港澳台）。
``?province=华南`` 天然就是「一次查多省」；而「港澳台」正是这个口径下的一个 region，
于是「查港澳台」不需要任何特殊逻辑。所以解析结果统一是**代码列表**而不是单个代码。

为什么把纯逻辑与取数分开
------------------------
:func:`resolve_codes` 只依赖传入的行数据，不碰数据库——
这样单测可以直接喂一个写死的行列表，不需要起库。
:func:`load_rows` 负责取数并做进程内缓存。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from newsplatform.core.exceptions import BadRequest

__all__ = [
    "normalize_key",
    "resolve_codes",
    "resolve_code",
    "load_rows",
    "invalidate_cache",
    "PROVINCE_HINT",
]

#: 需要剥离的后缀。**长的必须在前面**，否则 ``特别行政区`` 会先被 ``区`` 之类截断。
_SUFFIXES: tuple[str, ...] = (
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "特别行政区",
    "自治区",
    "自治州",
    "省",
    "市",
    "地区",
)

#: 俗称 / 简写 → 代码。只放「按后缀规则归一化后仍然对不上」的那些。
_ALIASES: dict[str, str] = {
    "内蒙": "150000",  # 字典里是「内蒙古」，直写「内蒙」对不上
    "内蒙古": "150000",
    "广西省": "450000",
    "新彊": "650000",  # 「疆」的常见错字，容错收下
    "宁夏省": "640000",
    "西藏省": "540000",
    "新疆省": "650000",
    "北京市": "110000",
    "京": "110000",
    "津": "120000",
    "沪": "310000",
    "渝": "500000",
    "粤": "440000",
    "苏": "320000",
    "浙": "330000",
    "鲁": "370000",
    "豫": "410000",
    "鄂": "420000",
    "湘": "430000",
    "川": "510000",
    "蜀": "510000",
    "黔": "520000",
    "滇": "530000",
    "云": "530000",
    "陕": "610000",
    "秦": "610000",
    "甘": "620000",
    "陇": "620000",
    "青": "630000",
    "闽": "350000",
    "皖": "340000",
    "赣": "360000",
    "冀": "130000",
    "晋": "140000",
    "辽": "210000",
    "吉": "220000",
    "黑": "230000",
    "琼": "460000",
}

#: 「不加省份过滤」的写法。
_NO_FILTER: frozenset[str] = frozenset({"全国", "全部", "所有", "不限", "all", "*", "-"})

#: 解析失败时附在错误消息后面的提示。
PROVINCE_HINT = (
    "可用取值：6 位代码（440000）、全称（广东省）、简称（广东）、"
    "大区（华北/东北/华东/华中/华南/西南/西北/港澳台），"
    "或不加过滤（全国）。完整字典见 GET /api/v1/provinces"
)

# 进程内缓存：省份字典是静态的 34 行，没必要每个请求都查一次。
_CACHE_TTL_SECONDS = 300.0
_CACHE: dict[str, Any] = {"rows": None, "expire_at": 0.0}


def normalize_key(value: str) -> str:
    """归一化名称：去空白 → 转小写 → 剥后缀。

    两侧（用户输入 与 字典里的 name/short_name）都要过这个函数，
    这样比较才是对称的——不必为「广西壮族自治区」这种特例写一堆映射。
    """
    text = "".join(str(value).split())  # 去首尾与内部的所有空白（含全角空格）
    text = text.casefold()
    changed = True
    while changed and text:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def build_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """把省份行编译成 ``归一化名称 → 代码列表`` 的索引。

    同一个 key 可能对应多个代码——大区名（如「华南」）就是一对多。
    """
    index: dict[str, list[str]] = {}

    def put(key: str, code: str) -> None:
        key = normalize_key(key)
        if not key:
            return
        bucket = index.setdefault(key, [])
        if code not in bucket:
            bucket.append(code)

    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        put(code, code)  # 代码本身也是一个 key
        for field in ("name", "short_name", "region"):
            value = row.get(field)
            if value:
                put(str(value), code)

    for alias, code in _ALIASES.items():
        put(alias, code)

    for codes in index.values():
        codes.sort()
    return index


def resolve_codes(value: str | None, rows: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """把用户输入解析成省份代码列表。

    返回值语义（**三态，别搞混**）:

    - ``None`` —— 表示「不加省份过滤」（输入 ``全国`` / ``all`` / 空）
    - ``["440000"]`` —— 单个省份
    - ``["710000", "810000", "820000"]`` —— 大区（如 ``港澳台`` / ``华南``）

    解析不出来时抛 :class:`BadRequest`（HTTP 400，业务码 1000），
    消息里会带上全部可用取值。
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    if normalize_key(raw) in _NO_FILTER or raw.casefold() in _NO_FILTER:
        return None

    index = build_index(rows)
    key = normalize_key(raw)
    codes = index.get(key)
    if not codes:
        # 再试一次原文（覆盖 normalize 把有用信息剥掉的极端情况，如输入就是「区」）
        codes = index.get(raw.casefold())
    if not codes:
        raise BadRequest(f"无法识别的省份：{raw}。{PROVINCE_HINT}")
    return list(codes)


def resolve_code(value: str | None, rows: Sequence[Mapping[str, Any]]) -> str:
    """解析成**唯一**一个省份代码（用于 ``/provinces/{code}`` 这种单值场景）。

    输入是大区名（一对多）时明确报错，而不是随手取第一个——
    静默挑一个会让用户以为查到了正确的省。
    """
    codes = resolve_codes(value, rows)
    if not codes:
        raise BadRequest("省份不能为空。")
    if len(codes) > 1:
        joined = "、".join(codes)
        raise BadRequest(
            f"「{value}」对应多个省级行政区（{joined}），"
            "该接口一次只能查一个省，请改用具体代码或名称。"
        )
    return codes[0]


async def load_rows(session: Any) -> list[dict[str, Any]]:
    """读取省份字典（带进程内 TTL 缓存）。

    用缓存而不是每个请求都查库：字典是静态的 34 行，且按名称筛选是高频动作。
    TTL 到期后自动重取，所以改了字典最多 5 分钟后生效，无需重启。
    """
    now = time.monotonic()
    cached = _CACHE.get("rows")
    if cached is not None and now < float(_CACHE.get("expire_at") or 0):
        return cached  # type: ignore[no-any-return]

    from newsplatform.repositories.sources import ProvinceRepository

    rows = await ProvinceRepository(session).list()
    _CACHE["rows"] = rows
    _CACHE["expire_at"] = now + _CACHE_TTL_SECONDS
    return rows


def invalidate_cache() -> None:
    """清掉缓存（测试与字典变更后使用）。"""
    _CACHE["rows"] = None
    _CACHE["expire_at"] = 0.0
