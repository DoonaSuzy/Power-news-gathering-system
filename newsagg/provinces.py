"""全国 34 个省级行政区字典（改造方案 §3.1）。

在原有 31 个省级行政区基础上补齐香港、澳门、台湾，行政区划代码遵循
GB/T 2260 / 国家统计局标准：

- 香港特别行政区 ``810000``
- 澳门特别行政区 ``820000``
- 台湾省 ``710000``

本模块是**唯一**的省份字典来源，采集层与数据中台均从此处取数，
避免两处各维护一份导致漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Province",
    "PROVINCES",
    "PROVINCE_BY_CODE",
    "PROVINCE_BY_NAME",
    "codes",
    "get",
    "get_by_name",
    "name_of",
    "all_names",
    "MAINLAND_COUNT",
    "TOTAL_COUNT",
]


@dataclass(frozen=True, slots=True)
class Province:
    """一个省级行政区。"""

    code: str          # 6 位行政区划代码
    name: str          # 全称（如「广西壮族自治区」）
    short: str         # 简称（如「广西」）
    region: str        # 大区归属：华北/东北/华东/华中/华南/西南/西北/港澳台
    is_special: bool = False  # 港澳台标记（`一国两制` 地区，采集需注意编码与合规）


PROVINCES: tuple[Province, ...] = (
    # ---------- 华北 ----------
    Province("110000", "北京市", "北京", "华北"),
    Province("120000", "天津市", "天津", "华北"),
    Province("130000", "河北省", "河北", "华北"),
    Province("140000", "山西省", "山西", "华北"),
    Province("150000", "内蒙古自治区", "内蒙古", "华北"),
    # ---------- 东北 ----------
    Province("210000", "辽宁省", "辽宁", "东北"),
    Province("220000", "吉林省", "吉林", "东北"),
    Province("230000", "黑龙江省", "黑龙江", "东北"),
    # ---------- 华东 ----------
    Province("310000", "上海市", "上海", "华东"),
    Province("320000", "江苏省", "江苏", "华东"),
    Province("330000", "浙江省", "浙江", "华东"),
    Province("340000", "安徽省", "安徽", "华东"),
    Province("350000", "福建省", "福建", "华东"),
    Province("360000", "江西省", "江西", "华东"),
    Province("370000", "山东省", "山东", "华东"),
    # ---------- 华中 ----------
    Province("410000", "河南省", "河南", "华中"),
    Province("420000", "湖北省", "湖北", "华中"),
    Province("430000", "湖南省", "湖南", "华中"),
    # ---------- 华南 ----------
    Province("440000", "广东省", "广东", "华南"),
    Province("450000", "广西壮族自治区", "广西", "华南"),
    Province("460000", "海南省", "海南", "华南"),
    # ---------- 西南 ----------
    Province("500000", "重庆市", "重庆", "西南"),
    Province("510000", "四川省", "四川", "西南"),
    Province("520000", "贵州省", "贵州", "西南"),
    Province("530000", "云南省", "云南", "西南"),
    Province("540000", "西藏自治区", "西藏", "西南"),
    # ---------- 西北 ----------
    Province("610000", "陕西省", "陕西", "西北"),
    Province("620000", "甘肃省", "甘肃", "西北"),
    Province("630000", "青海省", "青海", "西北"),
    Province("640000", "宁夏回族自治区", "宁夏", "西北"),
    Province("650000", "新疆维吾尔自治区", "新疆", "西北"),
    # ---------- 港澳台 ----------
    Province("710000", "台湾省", "台湾", "港澳台", is_special=True),
    Province("810000", "香港特别行政区", "香港", "港澳台", is_special=True),
    Province("820000", "澳门特别行政区", "澳门", "港澳台", is_special=True),
)

PROVINCE_BY_CODE: dict[str, Province] = {p.code: p for p in PROVINCES}
PROVINCE_BY_NAME: dict[str, Province] = {p.name: p for p in PROVINCES}
PROVINCE_BY_NAME.update({p.short: p for p in PROVINCES})

#: 大陆 31 个省级行政区（不含港澳台）
MAINLAND_COUNT = sum(1 for p in PROVINCES if not p.is_special)
#: 全部 34 个省级行政区
TOTAL_COUNT = len(PROVINCES)


def codes() -> list[str]:
    """全部行政区划代码（34 个）。"""
    return [p.code for p in PROVINCES]


def get(code: str) -> Province | None:
    """按代码取省份，兼容传入 int / 带空白。"""
    return PROVINCE_BY_CODE.get(str(code).strip())


def get_by_name(name: str) -> Province | None:
    """按全称或简称取省份。"""
    return PROVINCE_BY_NAME.get(name.strip())


def name_of(code: str) -> str:
    """代码 → 全称；未知返回空串。"""
    p = get(code)
    return p.name if p else ""


def all_names() -> list[str]:
    """全部省份全称（34 个）。"""
    return [p.name for p in PROVINCES]
