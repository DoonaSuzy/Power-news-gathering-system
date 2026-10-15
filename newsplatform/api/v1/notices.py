"""公告栏接口：**只读服务端缓存**，不触发 LLM 调用。

这样设计的直接效果：无论多少访客、刷新多少次页面，LLM 调用次数只取决于
服务端的定时刷新频率（默认 2 小时一次），与访客量**完全解耦**。

端点
----
- ``GET /api/v1/notices``          读缓存（0 次 LLM）
- ``GET /api/v1/notices/status``   缓存与调度状态（调试/验证用）

缓存由**启动预热 + 定时任务**（``POWERNEWS_NOTICE_INTERVAL_SECONDS``，默认 2 小时）刷新，
**没有手动刷新端点** —— 2026-10-16 核实：本文件的 docstring 曾列出
``POST /api/v1/notices/refresh``，但该路由自首次提交起就从未实现，
刻意以「无写入口」保证访客量完全不影响 LLM 调用次数。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.notice_service import get_notice_service

__all__ = ["router"]

router = APIRouter(
    prefix="/notices",
    tags=["notices"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)


@router.get("", summary="公告栏数据（读服务端缓存，不调 LLM）")
async def read_notices() -> dict:
    """返回预生成的公告数据。

    ``ready=false`` 表示服务刚启动、预热尚未完成 —— 这是**正常中间态**，
    调用方应提示「公告生成中」而不是报错。
    """
    data = await get_notice_service().get()
    return ok(data)


@router.get("/status", summary="公告缓存与定时任务状态")
async def notice_status() -> dict:
    """给运维与验证用：缓存是否就绪、下次刷新时刻、LLM 是否配置。"""
    return ok(get_notice_service().status())
