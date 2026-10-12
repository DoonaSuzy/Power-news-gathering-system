"""适配器注册中心。

一源一文件，模块导入即注册。新增来源只需三步：

1. 在 ``newsagg/adapters/<province>/<source_id>.py`` 写适配器类；
2. 在文件末尾调用 :func:`register`；
3. 把模块名加进本文件的 :data:`ADAPTER_MODULES`。

注册中心会做一致性自检：适配器的 ``source_id`` 必须能在
``sources_seed.SOURCES`` 里找到，否则报错——防止「代码里有适配器、
来源清单里没有」的漂移。
"""

from __future__ import annotations

from newsagg.adapters.base import BaseAdapter
from newsagg.errors import AdapterNotFound

__all__ = [
    "register",
    "get_adapter",
    "has_adapter",
    "available_adapters",
    "all_adapters",
    "ensure_loaded",
    "ADAPTER_MODULES",
]

_REGISTRY: dict[str, BaseAdapter] = {}

#: 需要导入以完成注册的适配器模块（按省份分组）
ADAPTER_MODULES: tuple[str, ...] = (
    # ---------- 华北 ----------
    "newsagg.adapters.beijing.sgcc_bj",
    "newsagg.adapters.tianjin.sgcc_tj",
    "newsagg.adapters.hebei.sgcc_he",
    "newsagg.adapters.hebei.sgcc_jb",
    "newsagg.adapters.shanxi.sgcc_sx",
    "newsagg.adapters.neimenggu.sgcc_md",
    "newsagg.adapters.neimenggu.impc",
    # ---------- 东北 ----------
    "newsagg.adapters.liaoning.sgcc_ln",
    "newsagg.adapters.jilin.sgcc_jl",
    "newsagg.adapters.heilongjiang.sgcc_hl",
    # ---------- 华东 ----------
    "newsagg.adapters.shanghai.sgcc_sh",
    "newsagg.adapters.jiangsu.sgcc_js",
    "newsagg.adapters.zhejiang.sgcc_zj",
    "newsagg.adapters.anhui.sgcc_ah",
    "newsagg.adapters.fujian.sgcc_fj",
    "newsagg.adapters.jiangxi.sgcc_jx",
    "newsagg.adapters.shandong.sgcc_sd",
    # ---------- 华中 ----------
    "newsagg.adapters.henan.sgcc_ha",
    "newsagg.adapters.hubei.sgcc_hb",
    "newsagg.adapters.hunan.sgcc_hn",
    # ---------- 华南 ----------
    "newsagg.adapters.guangdong.csg_gd",
    "newsagg.adapters.guangxi.csg_gx",
    "newsagg.adapters.hainan.csg_hn",
    # ---------- 西南 ----------
    "newsagg.adapters.chongqing.sgcc_cq",
    "newsagg.adapters.sichuan.sgcc_sc",
    "newsagg.adapters.guizhou.csg_gz",
    "newsagg.adapters.yunnan.csg_yn",
    "newsagg.adapters.xizang.sgcc_xz",
    # ---------- 西北 ----------
    "newsagg.adapters.shaanxi.sgcc_sn",
    "newsagg.adapters.gansu.sgcc_gs",
    "newsagg.adapters.qinghai.sgcc_qh",
    "newsagg.adapters.ningxia.sgcc_nx",
    "newsagg.adapters.xinjiang.sgcc_xj",
    # ---------- 港澳台 ----------
    "newsagg.adapters.hongkong.clp_hk",
    "newsagg.adapters.macao.cem_macau",
    "newsagg.adapters.taiwan.taipower",
    # ---------- 行业补充 ----------
    "newsagg.adapters.national.cpnn",
    "newsagg.adapters.national.bjx",
    "newsagg.adapters.national.in_en",
    # anysearch 检索发现的补充来源（2026-10-12 新增，各站一个专属适配器）
    "newsagg.adapters.national.nengyuanjie",
    "newsagg.adapters.national.chinapower",
    "newsagg.adapters.national.china5e",
    # 电力央企官网（2026-10-12 新增）
    "newsagg.adapters.national.cr_power",
    "newsagg.adapters.national.china_cdt",
    "newsagg.adapters.national.ctg",
    # ---------- 政府能源主管部门补充（gov）----------
    "newsagg.adapters.gov.gov_gz_nyj",
    "newsagg.adapters.gov.gov_gx_fgw",
    "newsagg.adapters.gov.gov_yn_nyj",
    "newsagg.adapters.gov.gov_sd_nyj",
    "newsagg.adapters.gov.gov_jx_nyj",
    "newsagg.adapters.gov.gov_hb_nyj",
)

_loaded = False


def register(adapter: BaseAdapter) -> BaseAdapter:
    """注册一个适配器实例。"""
    if not getattr(adapter, "source_id", ""):
        raise ValueError(f"{type(adapter).__name__} 缺少 source_id")
    _REGISTRY[adapter.source_id] = adapter
    return adapter


def get_adapter(source_id: str) -> BaseAdapter:
    """按 source_id 取适配器（未注册抛 ``AdapterNotFound``）。"""
    ensure_loaded()
    try:
        return _REGISTRY[source_id]
    except KeyError as exc:
        raise AdapterNotFound(f"未注册的适配器: {source_id}") from exc


def has_adapter(source_id: str) -> bool:
    ensure_loaded()
    return source_id in _REGISTRY


def available_adapters() -> list[str]:
    """全部已注册的 source_id（有序）。"""
    ensure_loaded()
    return sorted(_REGISTRY)


def all_adapters() -> dict[str, BaseAdapter]:
    """全部适配器（副本）。"""
    ensure_loaded()
    return dict(_REGISTRY)


def _load_all() -> None:
    """导入全部适配器模块以完成注册。"""
    import importlib

    for module in ADAPTER_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError:
            # 允许在渐进开发过程中缺失部分模块
            continue


def ensure_loaded() -> None:
    """确保全部适配器已导入（幂等）。"""
    global _loaded
    if not _loaded:
        _load_all()
        _loaded = True
