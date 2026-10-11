"""登录态管理：AuthProvider 协议、凭据结构，以及 cookies/token 过期自动获取机制。

核心类 :class:`CookieTokenManager` 包装一个 HTTP 会话，在每次请求前
（``before_request``）检查凭据是否临近过期、在每次响应后（``handle_response``）
检测 401/403（或页面提示"登录已过期"），一旦命中即调用
:class:`AuthProvider` 的 ``login()`` 自动重新登录并刷新凭据，随后由调用方重试
原请求——即"cookies/token 过期自动获取机制"。

会话与响应均为可注入的 Protocol，测试中以 Fake 实现替换，全程离线。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: 可注入时钟（默认 ``time.time``），测试中可冻结时间实现确定性过期判定。
Clock = Callable[[], float]

#: 视为"登录态失效"的 HTTP 状态码（401/403 为通用约定，419 为部分站点的过期码）。
EXPIRED_STATUS_CODES = frozenset({401, 403, 419})

#: 响应文本中出现这些关键词时同样判定为登录态过期（站点自定义提示）。
_EXPIRY_TEXT_HINTS = ("登录已过期", "登录过期", "token expired", "重新登录")


@runtime_checkable
class HttpResponse(Protocol):
    """HTTP 响应协议（兼容 requests.Response 形态）。"""

    status_code: int
    text: str

    def json(self) -> Any: ...


@runtime_checkable
class HttpSession(Protocol):
    """HTTP 会话协议：只依赖 ``get``，测试中以 Fake 会话注入。"""

    cookies: dict[str, str]

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class Credentials:
    """一次登录获得的凭据：cookies + token + 过期时间（epoch 秒）。"""

    cookies: dict[str, str] = field(default_factory=dict)
    token: str = ""
    #: 绝对过期时刻（epoch 秒）；<=0 表示永久有效
    expires_at: float = 0.0

    def cookie_header(self) -> str:
        """拼接 ``Cookie`` 请求头。"""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    def is_valid_at(self, now: float, *, skew: float = 0.0) -> bool:
        """判断在 ``now`` 时刻是否仍然有效（``skew`` 为提前刷新的余量）。"""
        if not self.token and not self.cookies:
            return False
        if self.expires_at <= 0:
            return True
        return now < self.expires_at - max(skew, 0.0)


@runtime_checkable
class AuthProvider(Protocol):
    """登录提供者协议：执行站点登录并返回新鲜凭据。"""

    name: str

    def login(self) -> Credentials:
        """登录目标站点，返回包含 cookies/token/expires_at 的凭据。"""
        ...

class FakeAuthProvider:
    """离线测试用登录提供者：可配置 token 模板、ttl 与登录二维码/验证码外发。

    - 每次 ``login()`` 递增计数并生成 ``token_template.format(n=count)``；
    - 若绑定 ``notifier``，登录前会先把站点返回的登录二维码（PNG 字节）与
      验证码以邮件方式发送给运维——模拟"远程扫码/读码登录"流程；
    - 不发起任何真实网络请求。
    """

    def __init__(
        self,
        *,
        name: str = "fake_finance_site",
        token_template: str = "tok-{n}",
        token_start: int = 1,
        cookies: dict[str, str] | None = None,
        ttl: float = 3600.0,
        clock: Clock | None = None,
        notifier: Any = None,
        qr_png: bytes | None = None,
        captcha_code: str | None = None,
    ) -> None:
        self.name = name
        self._token_template = token_template
        self._token_start = token_start
        self._cookies = dict(cookies) if cookies else {"SESSION": "sess-abc123"}
        self._ttl = ttl
        self._clock = clock or time.time
        self._notifier = notifier
        self._qr_png = qr_png
        self._captcha_code = captcha_code
        self.login_count = 0
        self.logins: list[Credentials] = []
        self.qr_sent = 0
        self.captcha_sent = 0

    def login(self) -> Credentials:
        self.login_count += 1
        self._relay_login_artifacts()
        creds = Credentials(
            cookies=dict(self._cookies),
            token=self._token_template.format(n=self.login_count + self._token_start - 1),
            expires_at=self._clock() + self._ttl,
        )
        self.logins.append(creds)
        return creds

    def _relay_login_artifacts(self) -> None:
        """把站点登录页的二维码/验证码通过邮件发给运维，等待远程确认。"""
        if self._notifier is None:
            return
        if self._qr_png is not None:
            self._notifier.send_login_qr(self._qr_png)
            self.qr_sent += 1
        if self._captcha_code is not None:
            self._notifier.send_captcha(self._captcha_code)
            self.captcha_sent += 1


class CookieTokenManager:
    """cookies/token 过期自动获取机制：请求前检查、响应后检测、自动重新登录。

    参数
    ----
    provider:
        :class:`AuthProvider` 实现，负责真正的登录。
    session:
        可选的 :class:`HttpSession`；登录成功后把 cookies 写回会话。
    clock:
        可注入时钟（默认 ``time.time``）。
    refresh_skew_seconds:
        提前刷新余量：距过期不足该秒数即视为过期。
    """

    def __init__(
        self,
        provider: AuthProvider,
        *,
        session: HttpSession | None = None,
        clock: Clock | None = None,
        refresh_skew_seconds: float = 60.0,
        source: str = "finance",
    ) -> None:
        self._provider = provider
        self._session = session
        self._clock = clock or time.time
        self._refresh_skew_seconds = refresh_skew_seconds
        self.source = source
        self._creds: Credentials | None = None
        #: 自动重新登录次数（供断言"401 恰好触发一次重新登录"）。
        self.refresh_count = 0

    # ------------------------------------------------------------------ 查询

    @property
    def credentials(self) -> Credentials | None:
        """当前凭据（未登录时为 None）。"""
        return self._creds

    def is_expired(self) -> bool:
        """当前凭据是否缺失或临近过期。"""
        return self._creds is None or not self._creds.is_valid_at(
            self._clock(), skew=self._refresh_skew_seconds
        )

    def prime(self, creds: Credentials) -> None:
        """从持久化状态（或测试）预置凭据，不触发登录。"""
        self._creds = creds
        self._apply_to_session(creds)

    # ------------------------------------------------------------------ 刷新

    def refresh(self) -> Credentials:
        """调用 provider.login() 重新登录并刷新凭据。"""
        self._creds = self._provider.login()
        self.refresh_count += 1
        self._apply_to_session(self._creds)
        return self._creds

    def before_request(self) -> Credentials:
        """请求前调用：临近过期则自动重新登录，返回当前可用凭据。"""
        if self.is_expired():
            self.refresh()
        assert self._creds is not None  # refresh 后必然非空
        return self._creds

    def handle_response(self, response: HttpResponse) -> bool:
        """请求后调用：检测 401/403（或页面过期提示）并自动重新登录。

        返回 ``True`` 表示发生了刷新、调用方应当用新凭据重试原请求。
        """
        if self._response_indicates_expiry(response):
            self.refresh()
            return True
        return False

    def headers(self) -> dict[str, str]:
        """基于当前凭据生成请求头（Cookie + Bearer token）。"""
        creds = self._creds or Credentials()
        headers: dict[str, str] = {}
        cookie_header = creds.cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        if creds.token:
            headers["Authorization"] = f"Bearer {creds.token}"
        return headers

    # ------------------------------------------------------------------ 内部

    def _response_indicates_expiry(self, response: HttpResponse) -> bool:
        if getattr(response, "status_code", 200) in EXPIRED_STATUS_CODES:
            return True
        text = (getattr(response, "text", "") or "").lower()
        return any(hint in text for hint in _EXPIRY_TEXT_HINTS)

    def _apply_to_session(self, creds: Credentials) -> None:
        session = self._session
        if session is None:
            return
        cookies = getattr(session, "cookies", None)
        if isinstance(cookies, dict):
            cookies.update(creds.cookies)


__all__ = [
    "AuthProvider",
    "Clock",
    "CookieTokenManager",
    "Credentials",
    "EXPIRED_STATUS_CODES",
    "FakeAuthProvider",
    "HttpResponse",
    "HttpSession",
]

