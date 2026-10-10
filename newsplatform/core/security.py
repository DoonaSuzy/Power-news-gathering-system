"""鉴权依赖（改造方案 §2.2 ``@login_required`` → ``Depends(require_auth)``、§6.3）。

认证方式：**Bearer Token**
    客户端 ``Authorization: Bearer <key>``；服务端只存 ``sha256(key)``，
    比对哈希而不是明文，库被读走也无法直接冒用。

授权方式：**scope 白名单**
    ``articles:read`` / ``sources:read`` / ``sources:write`` / ``admin``。
    ``admin`` 隐含全部权限。写接口一律要求 ``*:write`` 或 ``admin``。

引导机制
    库内一条 Key 都没有时，可用 ``POWERNEWS_PLATFORM_BOOTSTRAP_ADMIN_TOKEN``
    访问，便于首次调用 ``POST /api/v1/admin/api-keys`` 创建正式 Key。
    一旦库里存在任意一条 Key，引导令牌立即失效——避免留后门。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from newsplatform.core.config import PlatformSettings, get_platform_settings
from newsplatform.core.context import current_context
from newsplatform.core.database import get_platform_session
from newsplatform.core.exceptions import Forbidden, Unauthorized
from newsplatform.repositories.admin import ApiKeyRepository

__all__ = [
    "hash_api_key",
    "generate_api_key",
    "AuthContext",
    "require_auth",
    "require_scope",
    "require_admin",
]


def hash_api_key(raw_key: str) -> str:
    """``sha256(明文)`` 十六进制（与 ``api_keys.key_hash`` 同长 64）。"""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "pn") -> str:
    """生成明文 Key：``pn_<43 位 urlsafe>``。"""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


class AuthContext:
    """认证结果。"""

    __slots__ = ("key_id", "name", "scopes", "is_bootstrap")

    def __init__(self, key_id: int | None, name: str, scopes: set[str],
                 *, is_bootstrap: bool = False) -> None:
        self.key_id = key_id
        self.name = name
        self.scopes = scopes
        self.is_bootstrap = is_bootstrap

    def has(self, scope: str) -> bool:
        """是否具备某权限（``admin`` 隐含全部）。"""
        return "admin" in self.scopes or scope in self.scopes

    def __repr__(self) -> str:  # pragma: no cover - 日志友好
        return f"AuthContext(name={self.name!r}, scopes={sorted(self.scopes)})"


async def require_auth(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    session: AsyncSession = Depends(get_platform_session),
    settings: PlatformSettings = Depends(get_platform_settings),
) -> AuthContext:
    """校验 Bearer Token，返回 :class:`AuthContext`。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("缺少 Bearer Token（Authorization: Bearer <key>）")
    raw = authorization.split(" ", 1)[1].strip()
    if not raw:
        raise Unauthorized("Token 为空")

    repo = ApiKeyRepository(session)

    # 引导令牌：仅当库内还没有任何 Key 时可用
    bootstrap = (settings.bootstrap_admin_token or "").strip()
    if bootstrap and secrets.compare_digest(raw, bootstrap):
        if await repo.count() == 0:
            ctx = AuthContext(None, "bootstrap", {"admin"}, is_bootstrap=True)
            current_context().api_key_name = ctx.name
            return ctx
        raise Forbidden("引导令牌已失效：库内已存在 API Key，请使用正式 Key")

    key_hash = hash_api_key(raw)
    record = await repo.get_by_hash(key_hash)
    if record is None:
        raise Unauthorized("Token 无效")
    if not record.enabled:
        raise Forbidden("该 API Key 已停用")
    if record.expires_at and record.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise Forbidden("该 API Key 已过期")

    ctx = AuthContext(record.id, record.name, record.scope_set())
    current_context().api_key_name = ctx.name
    await repo.touch(record.id)
    return ctx


def require_scope(scope: str):
    """生成一个要求指定 scope 的依赖。"""

    async def _dep(auth: AuthContext = Depends(require_auth)) -> AuthContext:
        if not auth.has(scope):
            raise Forbidden(f"缺少权限: {scope}")
        return auth

    return _dep


async def require_admin(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    """要求 ``admin`` 权限。"""
    if not auth.has("admin"):
        raise Forbidden("需要 admin 权限")
    return auth
