"""省份「名称 → 行政区划代码」解析的回归测试。

同样**不需要数据库**：:func:`resolve_codes` 只依赖传入的行数据，
所以这里喂一份写死的行列表就够了（真是设计成这样的，见模块 docstring）。

钉住的核心契约：
1. 三态返回值（``None`` / 单省 / 大区）不能被谁改成两态；
2. 后缀剥离与别名必须对称 —— 用户写「广东」和「广东省」得同一个结果；
3. **解析失败必须抛错**，绝不能静默返回空列表。
   这条最要紧：``/sources?province=`` 曾经压根不校验，传「广东」会
   **静默返回 0 条**，看起来像「这个省确实没数据」，比报错难查得多。
"""

from __future__ import annotations

import pytest

from newsplatform.core.exceptions import BadRequest
from newsplatform.core.provinces import (
    PROVINCE_HINT,
    build_index,
    normalize_key,
    resolve_code,
    resolve_codes,
)
from newsplatform.repositories.base import escape_like

# 真实字典（GET /api/v1/provinces）的剪裁版：覆盖直辖市、自治区、普通省、
# 大区一对多（华南）、以及港澳台这三个特别行政区。
_ROWS: list[dict[str, object]] = [
    {"code": "110000", "name": "北京市", "short_name": "北京", "region": "华北", "is_special": False},
    {"code": "130000", "name": "河北省", "short_name": "河北", "region": "华北", "is_special": False},
    {"code": "140000", "name": "山西省", "short_name": "山西", "region": "华北", "is_special": False},
    {"code": "150000", "name": "内蒙古自治区", "short_name": "内蒙古", "region": "华北", "is_special": False},
    {"code": "310000", "name": "上海市", "short_name": "上海", "region": "华东", "is_special": False},
    {"code": "440000", "name": "广东省", "short_name": "广东", "region": "华南", "is_special": False},
    {"code": "450000", "name": "广西壮族自治区", "short_name": "广西", "region": "华南", "is_special": False},
    {"code": "460000", "name": "海南省", "short_name": "海南", "region": "华南", "is_special": False},
    {"code": "540000", "name": "西藏自治区", "short_name": "西藏", "region": "西南", "is_special": False},
    {"code": "650000", "name": "新疆维吾尔自治区", "short_name": "新疆", "region": "西北", "is_special": False},
    {"code": "710000", "name": "台湾省", "short_name": "台湾", "region": "港澳台", "is_special": True},
    {"code": "810000", "name": "香港特别行政区", "short_name": "香港", "region": "港澳台", "is_special": True},
    {"code": "820000", "name": "澳门特别行政区", "short_name": "澳门", "region": "港澳台", "is_special": True},
]


# ======================================================================
# 1. normalize_key：后缀剥离 + 空白 + 大小写
# ======================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("广东省", "广东"),
        ("广东", "广东"),
        (" 广东 ", "广东"),
        ("广 东", "广东"),          # 内部空白也要吃掉
        ("广东\u3000省", "广东"),    # 全角空格
        ("北京市", "北京"),
        ("内蒙古自治区", "内蒙古"),
        ("广西壮族自治区", "广西"),
        ("新疆维吾尔自治区", "新疆"),
        ("宁夏回族自治区", "宁夏"),
        ("香港特别行政区", "香港"),
        ("ALL", "all"),             # casefold 而非 lower
    ],
)
def test_normalize_strips_suffix_and_whitespace(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


def test_normalize_longest_suffix_wins() -> None:
    """后缀按「长优先」匹配。

    若 ``自治区`` 排在 ``壮族自治区`` 前面，「广西壮族自治区」会被切成
    「广西壮」而不是「广西」——所以顺序是有意义的，不是随便排的。
    """
    assert normalize_key("广西壮族自治区") == "广西"
    assert normalize_key("新疆维吾尔自治区") == "新疆"


def test_normalize_keeps_bare_suffix_intact() -> None:
    """输入本身就等于后缀时不能剥成空串（``len(text) > len(suffix)`` 的守卫）。"""
    assert normalize_key("省") == "省"
    assert normalize_key("市") == "市"


# ======================================================================
# 2. build_index：一对多（大区）
# ======================================================================
def test_build_index_maps_region_to_multiple_codes() -> None:
    index = build_index(_ROWS)
    assert index["华南"] == ["440000", "450000", "460000"]
    assert index["港澳台"] == ["710000", "810000", "820000"]


def test_build_index_uses_name_short_name_and_code() -> None:
    """三种 key 都要能命中：代码 / 全称 / 简称。"""
    index = build_index(_ROWS)
    assert index["440000"] == ["440000"]
    assert index["广东"] == ["440000"]      # short_name 与 name 剥后缀后同 key
    assert index["北京"] == ["110000"]


def test_build_index_deduplicates() -> None:
    """同一 key 不能出现重复代码（name 与 short_name 归一化后常常撞一起）。"""
    index = build_index(_ROWS)
    for key, codes in index.items():
        assert len(codes) == len(set(codes)), f"{key} 存在重复代码：{codes}"
        assert codes == sorted(codes), f"{key} 的代码未排序：{codes}"


# ======================================================================
# 3. resolve_codes：三态返回
# ======================================================================
@pytest.mark.parametrize("raw", [None, "", "   ", "全国", "全部", "所有", "不限", "all", "ALL", "*", "-"])
def test_resolve_codes_returns_none_for_no_filter(raw: str | None) -> None:
    """``None`` 表示「不加省份过滤」，与「查不到」是两回事。"""
    assert resolve_codes(raw, _ROWS) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("440000", ["440000"]),
        ("广东省", ["440000"]),
        ("广东", ["440000"]),
        ("粤", ["440000"]),              # 单字简称
        ("华南", ["440000", "450000", "460000"]),
        ("港澳台", ["710000", "810000", "820000"]),
        ("香港", ["810000"]),
        ("内蒙", ["150000"]),            # 字典里没有「内蒙」这个 short_name，靠别名收
        ("内蒙古", ["150000"]),
        ("广西省", ["450000"]),          # 现实里常见的错误写法
        ("新彊", ["650000"]),            # 「疆」的错字
        (" 广东 ", ["440000"]),
    ],
)
def test_resolve_codes_success(raw: str, expected: list[str]) -> None:
    assert resolve_codes(raw, _ROWS) == expected


def test_resolve_codes_raises_on_unknown_and_lists_choices() -> None:
    """解析失败**必须抛错**，且消息里要有可用取值提示。

    这是本轮修的核心缺陷：以前不校验 → 静默 0 条。
    """
    with pytest.raises(BadRequest) as exc:
        resolve_codes("米国", _ROWS)

    message = str(exc.value)
    assert "米国" in message
    assert PROVINCE_HINT in message
    assert "440000" in message, "错误提示里应当带上示例代码"


def test_resolve_codes_copies_the_result() -> None:
    """返回的是副本，调用方改它不会污染内部索引。"""
    first = resolve_codes("华南", _ROWS)
    assert first is not None
    first.append("999999")

    second = resolve_codes("华南", _ROWS)
    assert second == ["440000", "450000", "460000"]


# ======================================================================
# 4. resolve_code：单值场景，一对多必须报错
# ======================================================================
def test_resolve_code_returns_single_code() -> None:
    assert resolve_code("广东", _ROWS) == "440000"
    assert resolve_code("440000", _ROWS) == "440000"


def test_resolve_code_rejects_region_name() -> None:
    """大区名落到单值接口上要**明确报错**，不能随手挑第一个。

    静默取 ``codes[0]`` 会让用户以为查到了「华南」这个省——
    实际上返回的只是广东。这类「看起来成功」的错误最难发现。
    """
    with pytest.raises(BadRequest) as exc:
        resolve_code("华南", _ROWS)

    message = str(exc.value)
    assert "多个" in message
    assert "440000" in message and "450000" in message and "460000" in message


def test_resolve_code_rejects_empty() -> None:
    with pytest.raises(BadRequest, match="不能为空"):
        resolve_code(None, _ROWS)


# ======================================================================
# 5. LIKE 通配符转义
# ======================================================================
def test_escape_like_escapes_wildcards() -> None:
    """``%`` / ``_`` 必须被转义，否则用户搜「100%新能源」会命中任意串。

    ``%`` 被当成「匹配任意字符」时，搜「100%新能源」会匹配到
    「100万千瓦新能源」这类完全不相关的结果——而且**不会报错**。
    """
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("normal") == "normal"


def test_escape_like_escapes_backslash_first() -> None:
    """反斜杠必须**最先**替换，否则后两步的转义符会被二次转义。

    顺序错了的话 ``\\`` 会变成 ``\\\\\\\\``，匹配行为直接崩掉。
    """
    assert escape_like("a\\b") == "a\\\\b"
    # 反斜杠 + 通配符的组合：转义符本身也要被转义
    assert escape_like("a\\%") == "a\\\\\\%"


def test_escape_like_handles_non_string() -> None:
    """非字符串输入先 ``str()``，不要在这里抛 TypeError。"""
    assert escape_like(123) == "123"


# ======================================================================
# 6. 查询对象契约：province_codes 是元组，且 ArticleQuery 是 slots
# ======================================================================
def test_article_query_uses_province_codes_tuple() -> None:
    """``ArticleQuery.province_codes`` 必须是 tuple。

    从单值 ``province_code`` 改成 ``province_codes`` 是为了让「华南」
    这种一对多的筛选能一次性传进去；tuple 而非 list 是因为 dataclass 要可哈希、
    且要能安全地放进 ``.in_()``。
    """
    from newsplatform.repositories.articles import ArticleQuery

    q = ArticleQuery(province_codes=("440000", "450000"))
    assert q.province_codes == ("440000", "450000")
    assert ArticleQuery().province_codes is None


def test_article_query_is_slots_dataclass() -> None:
    """``ArticleQuery`` 是 ``slots=True`` 的 dataclass。

    注意别把语义搞反：``slots`` **只禁止长出未声明的属性**，
    给已声明的字段（如 ``province_codes``）赋值本身是合法的。
    真正被挡住的是 ``q.某个临时状态 = x`` 这类把查询对象当便签用的写法。

    派生新实例仍然推荐 ``dataclasses.replace``：语义是「基于旧条件派生」，
    而不是原地改掉一个可能被别处共享的查询对象。
    """
    from dataclasses import fields, replace

    from newsplatform.repositories.articles import ArticleQuery

    assert "__slots__" in ArticleQuery.__dict__, "ArticleQuery 应当是 slots dataclass"
    assert "province_codes" in ArticleQuery.__slots__
    assert "province_codes" in {f.name for f in fields(ArticleQuery)}

    q = ArticleQuery()
    with pytest.raises(AttributeError):
        q.temporary_note = "x"  # type: ignore[attr-defined]

    derived = replace(q, province_codes=("440000",))
    assert derived.province_codes == ("440000",)
    assert q.province_codes is None, "replace 不能改动原实例"
