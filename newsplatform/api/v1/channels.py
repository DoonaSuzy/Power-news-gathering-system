"""数据域目录接口（``/api/v1/channels``）。

为什么要单独一个端点
--------------------
合并后 ``/articles?channel=`` 有三个取值，但客户端无法预知哪个域**真的有数据**。
没有这个端点，它只能先请求一次 ``channel=province`` 拿到 503 再回退——
把一个配置事实变成了运行时错误。这里一次性给出「有哪些域、各多少条、
最新一条是什么时候」，客户端据此决定要不要显示对应 tab。

返回里 ``available=false`` 的域也保留一行：**「存在但没接」和「不存在」
是两件事**，前者运维配一下就能用，后者永远用不了。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from newsplatform.core.deps import CollectorSessionDep, ProvinceSessionDep
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import require_scope
from newsplatform.services.channel_service import ChannelService

__all__ = ["router"]

router = APIRouter(
    prefix="/channels",
    tags=["channels"],
    dependencies=[Depends(require_scope("articles:read")), Depends(enforce_rate_limit)],
)


@router.get("", summary="数据域目录（规模与新鲜度）")
async def list_channels(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
) -> dict:
    """列出 power / province / all 三个数据域的来源数、文章数、最新发布时间。"""
    items = await ChannelService(session, province_session).catalog()
    return ok({"items": items, "total": len(items)})
