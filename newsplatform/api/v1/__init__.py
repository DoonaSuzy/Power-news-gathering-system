"""v1 路由聚合（改造方案 §2.2：对应 Flask 的 ``Blueprint`` 注册）。

版本化路径让未来 v2 可以并存（改造方案 §6.1「API版本化」）：
``/api/v1/...`` 与 ``/api/v2/...`` 各自挂一套 router，互不影响。
"""

from __future__ import annotations

from fastapi import APIRouter

from newsplatform.api.v1 import admin, articles, health, provinces, sources, stats

__all__ = ["api_router", "ROUTERS"]

#: 供文档与测试遍历
ROUTERS = (health, articles, provinces, sources, stats, admin)

api_router = APIRouter()
for _module in ROUTERS:
    api_router.include_router(_module.router)
