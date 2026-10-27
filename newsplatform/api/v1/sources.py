"""来源管理接口（改造方案 §3.6 的 ``/api/v1/sources`` 系列）。

权限分层
--------
- 读（``GET``）：需 ``sources:read``
- 写（``POST`` / ``PATCH`` / ``DELETE``）：需 ``sources:write``

写操作**一律经** :func:`audit_write` 留痕——谁在什么时候改了哪个来源、
成功还是失败（失败也要留痕，这正是用上下文管理器而非装饰器的原因）。

写入边界（重要）
----------------
中台只能改**配置列**（``enabled`` / ``list_url`` / ``crawl_interval_hours`` /
``priority`` / ``note`` / ``status``），白名单由 ``SourceRepository.UPDATABLE_FIELDS``
在代码层强制。产出行（``articles`` / ``crawl_jobs`` / ``article_tasks``）
永远由采集层独占写，中台碰它们是数据竞争的源头。

会话选择（踩过的坑）
--------------------
读接口用 ``CollectorSessionDep``（只读，退出不提交）；
写接口必须用 ``CollectorWriteSessionDep``。曾经 5 个写接口全都注入了只读会话，
结果接口返回 200 和自增 id，库里一行都没多——因为写入在会话关闭时被静默回滚。
现在提交由 ``SourceService`` 显式完成（保证审计日志与实际结果一致），
``CollectorWriteSessionDep`` 作为兜底。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from newsplatform.api.v1._audit import audit_write
from newsplatform.core.deps import (
    CATEGORY_CHOICES,
    CHANNEL_DESC,
    Channel,
    CollectorSessionDep,
    CollectorWriteSessionDep,
    PaginationDep,
    PlatformSessionDep,
    ProvinceSessionDep,
    resolve_province_codes,
)
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.response import ok
from newsplatform.core.security import AuthContext, require_scope
from newsplatform.models.schemas import SourceCreate, SourceUpdate
from newsplatform.services.channel_service import ChannelService
from newsplatform.services.source_service import SourceService

__all__ = ["router"]

router = APIRouter(
    prefix="/sources",
    tags=["sources"],
    dependencies=[Depends(enforce_rate_limit)],
)

ReadDep = Depends(require_scope("sources:read"))
WriteDep = Depends(require_scope("sources:write"))
#: 单例化的鉴权依赖。直接写成 ``auth: AuthContext = Depends(require_scope(...))``
#: 也是对的（FastAPI 的标准写法），但每次请求都会新建一个依赖对象；
#: 提到模块级后，签名里也不再出现「函数调用当默认值」这种形式。
WriteAuthDep = Depends(require_scope("sources:write"))


@router.get("", summary="来源列表（power/province）", dependencies=[ReadDep])
async def list_sources(
    session: CollectorSessionDep,
    province_session: ProvinceSessionDep,
    pagination: PaginationDep,
    channel: Annotated[Channel, Query(description=CHANNEL_DESC)] = "all",
    province: Annotated[
        str | None,
        Query(
            description=(
                "省份：代码 / 全称 / 简称 / 大区名均可。"
                "例：440000、广东省、广东、华南、港澳台、全国（不过滤）"
            ),
            examples=["广东"],
        ),
    ] = None,
    category: Annotated[str | None, Query(description="来源类别")] = None,
    enabled: Annotated[bool | None, Query(description="只看启用 / 停用")] = None,
    q: Annotated[
        str | None,
        Query(
            description="名称关键字模糊匹配（站点名称 / 适配器名 / 站点 id）",
            examples=["北京"],
        ),
    ] = None,
    keyword: Annotated[
        str | None,
        Query(
            description="``q`` 的别名（等价，与 ``/articles?keyword=`` 保持一致）。"
            "两个都给时以 ``q`` 为准。",
            examples=["电力"],
        ),
    ] = None,
) -> dict:
    """分页列出来源（按 priority → province → id 排序，与调度建批顺序一致）。

    ``province`` 走的是名称解析（代码 / 全称 / 简称 / 大区 / 全国）。
    原先这里**完全不校验**，传「广东」会静默返回 0 条——比报错更难发现，
    所以现在和 ``/articles`` 用同一套解析。

    ``q`` / ``keyword`` 互为别名，避免「换个接口就得换参数名」这种隐形门槛。
    """
    province_codes = await resolve_province_codes(session, province)
    payload = await ChannelService(session, province_session).sources(
        channel=channel,
        province_codes=tuple(province_codes) if province_codes else None,
        category=category,
        enabled=enabled,
        keyword=q or keyword,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return ok(payload)


@router.post("", summary="新增来源", dependencies=[WriteDep])
async def create_source(
    request: Request,
    session: CollectorWriteSessionDep,
    platform: PlatformSessionDep,
    payload: SourceCreate,
    auth: AuthContext = WriteAuthDep,
) -> dict:
    """登记一条来源配置。

    ⚠️ 新增来源**必须**先有对应适配器文件（``newsagg/adapters/<pkg>/<name>.py``），
    否则协调器建批后引擎会以 ``AdapterNotFound`` 失败。本接口只登记配置。
    """
    async with audit_write(
        platform, request, auth, detail=f"create source {payload.adapter_name}"
    ):
        item = await SourceService(session).create(payload.model_dump())
    return ok(item.model_dump(mode="json"))


@router.get("/{source_id}", summary="来源详情", dependencies=[ReadDep])
async def get_source(session: CollectorSessionDep, source_id: int) -> dict:
    """取单个来源；不存在返回业务码 3002（``source_not_found``）。"""
    item = await SourceService(session).get(source_id)
    return ok(item.model_dump(mode="json"))


@router.patch("/{source_id}", summary="更新来源配置", dependencies=[WriteDep])
async def update_source(
    request: Request,
    session: CollectorWriteSessionDep,
    platform: PlatformSessionDep,
    source_id: int,
    payload: SourceUpdate,
    auth: AuthContext = WriteAuthDep,
) -> dict:
    """只更新白名单内的配置列，其余字段静默忽略。"""
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with audit_write(
        platform, request, auth, detail=f"update source {source_id} {sorted(data)}"
    ):
        item = await SourceService(session).update(source_id, data)
    return ok(item.model_dump(mode="json"))


@router.post("/{source_id}/enable", summary="启用来源", dependencies=[WriteDep])
async def enable_source(
    request: Request,
    session: CollectorWriteSessionDep,
    platform: PlatformSessionDep,
    source_id: int,
    auth: AuthContext = WriteAuthDep,
) -> dict:
    """启用来源（最常用的运维动作，单开一个语义化端点便于审计检索）。"""
    async with audit_write(platform, request, auth, detail=f"enable source {source_id}"):
        item = await SourceService(session).set_enabled(source_id, True)
    return ok(item.model_dump(mode="json"))


@router.post("/{source_id}/disable", summary="停用来源", dependencies=[WriteDep])
async def disable_source(
    request: Request,
    session: CollectorWriteSessionDep,
    platform: PlatformSessionDep,
    source_id: int,
    auth: AuthContext = WriteAuthDep,
) -> dict:
    """停用来源（比删除安全：文章与任务留痕不丢）。"""
    async with audit_write(platform, request, auth, detail=f"disable source {source_id}"):
        item = await SourceService(session).set_enabled(source_id, False)
    return ok(item.model_dump(mode="json"))


@router.delete("/{source_id}", summary="删除来源", dependencies=[WriteDep])
async def delete_source(
    request: Request,
    session: CollectorWriteSessionDep,
    platform: PlatformSessionDep,
    source_id: int,
    auth: AuthContext = WriteAuthDep,
) -> dict:
    """删除来源。**该来源下已有文章时拒绝删除**（保数据可追溯），请改用停用。"""
    async with audit_write(platform, request, auth, detail=f"delete source {source_id}"):
        await SourceService(session).delete(source_id)
    return ok({"deleted": source_id})


#: 供 OpenAPI 文档展示可选值
CATEGORY_CHOICES_DOC = CATEGORY_CHOICES
