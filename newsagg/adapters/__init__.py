"""适配器注册表。

一源一文件，模块导入即注册。对外只暴露注册中心的四个函数，
调用方（蜘蛛 / 引擎 / CLI）不需要知道适配器在文件系统里的位置。

    from newsagg.adapters import get_adapter
    adapter = get_adapter("sgcc_bj")
    parsed = adapter.parse_list(html, base_url=adapter.list_url)
"""

from __future__ import annotations

from newsagg.adapters.base import (
    AdapterContext,
    BaseAdapter,
    DetailParse,
    ListItem,
    ListParse,
)
from newsagg.adapters.registry import (
    ADAPTER_MODULES,
    all_adapters,
    available_adapters,
    ensure_loaded,
    get_adapter,
    has_adapter,
    register,
)

__all__ = [
    "AdapterContext",
    "BaseAdapter",
    "DetailParse",
    "ListItem",
    "ListParse",
    "register",
    "get_adapter",
    "has_adapter",
    "available_adapters",
    "all_adapters",
    "ensure_loaded",
    "ADAPTER_MODULES",
]
