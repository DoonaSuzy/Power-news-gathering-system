"""金融数据爬虫模块离线测试。

覆盖简历三条核心诉求：
1. cookies/token 过期自动获取机制（401 → 恰好一次重新登录 → 重试成功）；
2. 邮件方式传输登录二维码 / 验证码（DryRun 落盘 outbox + Recording 捕获）；
3. 告警机制与远程恢复（失败立即告警并下发 restart 命令，恢复后重试成功）；
外加三个爬虫（Ing 现价 / CECI 进口煤指数 / 市场电价日报）端到端离线落库
（sqlite）与注册表校验。全程不发起任何真实网络 / SMTP / 数据库连接。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from newsagg.finance import (
    ALERT_SLA_SECONDS,
    FINANCE_SOURCES,
    AlertManager,
    BaseFinanceCrawler,
    CeciCoalIndexCrawler,
    CookieTokenManager,
    Credentials,
    DryRunTransport,
    FakeAuthProvider,
    FinanceCrawlError,
    IngSpotPriceCrawler,
    MarketPriceDailyCrawler,
    MySqlWriter,
    RecordingRecoveryTransport,
    RecordingTransport,
    SqliteWriter,
    available_sources,
    build_all,
    build_crawler,
    get_meta,
)
from newsagg.finance.alerting import Alert, RecoveryCommand
from newsagg.finance.emailer import EmailNotifier

# --------------------------------------------------------------------------- 夹具数据

#: 站点登录二维码（1x1 PNG，模拟站点返回的二维码图片字节）。
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
    "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

ING_LIST_HTML = """
<table class="price-list">
  <tr class="row"><td class="name">1#电解铜</td><td class="price">78,650</td>
    <td class="unit">元/吨</td><td class="date">2026-05-12</td>
    <td><a class="detail" href="/price/detail/cu1.html">详情</a></td></tr>
  <tr class="row"><td class="name">A00铝</td><td class="price">20,850</td>
    <td class="unit">元/吨</td><td class="date">2026-05-12</td>
    <td><a class="detail" href="/price/detail/al00.html">详情</a></td></tr>
</table>
"""

ING_DETAIL_CU1_HTML = """
<div class="spot-detail"><h1>1#电解铜</h1>
  <span class="price">78,650</span><span class="unit">元/吨</span>
  <span class="ts">2026-05-12 10:30</span></div>
"""

ING_DETAIL_AL00_HTML = """
<div class="spot-detail"><h1>A00铝</h1>
  <span class="price">20,850</span><span class="unit">元/吨</span>
  <span class="ts">2026-05-12 10:30</span></div>
"""

CECI_LIST_JSON: dict[str, Any] = {
    "code": 0,
    "data": {
        "items": [
            {
                "id": "ceci-5500k",
                "name": "CECI进口煤5500K",
                "url": "/ceci/api/detail/ceci-5500k.json",
            },
            {
                "id": "ceci-4700k",
                "name": "CECI进口煤4700K",
                "url": "/ceci/api/detail/ceci-4700k.json",
            },
        ]
    },
}

CECI_DETAIL_5500K_JSON: dict[str, Any] = {
    "code": 0,
    "data": {"name": "CECI进口煤5500K", "value": 812.5, "change": -3.2, "ts": "2026-05-11"},
}

CECI_DETAIL_4700K_JSON: dict[str, Any] = {
    "code": 0,
    "data": {"name": "CECI进口煤4700K", "value": 688.0, "change": 1.5, "ts": "2026-05-11"},
}

MARKET_LIST_HTML = """
<ul class="daily-list">
  <li><a href="/market/daily/2026-05-12.html">2026-05-12 市场电价日报</a></li>
</ul>
"""

MARKET_DETAIL_HTML = """
<div class="daily-detail"><span class="report-date">2026-05-12</span>
<table class="price-table">
  <tr><td class="region">甘肃</td><td class="price">0.3124</td></tr>
  <tr><td class="region">青海</td><td class="price">0.2987</td></tr>
</table></div>
"""


class FakeResponse:
    """内存响应：满足 HttpResponse 协议（status_code / text / json）。"""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeHttpSession:
    """内存会话：按路由返回响应，凭无效 token 的请求返回 401。

    - ``routes``: url -> FakeResponse 或 FakeResponse 列表（按序弹出）；
    - ``accepted_tokens``: 允许通过的 Bearer token 集合（模拟服务端登录态校验）。
    """

    def __init__(
        self,
        routes: dict[str, Any],
        *,
        accepted_tokens: set[str] | None = None,
        require_token: bool = True,
    ) -> None:
        self.routes = dict(routes)
        self.accepted_tokens = set(accepted_tokens or {"tok-1"})
        self.require_token = require_token
        self.cookies: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        headers = headers or {}
        self.requests.append({"url": url, "headers": dict(headers), "params": params})
        if self.require_token:
            authorization = headers.get("Authorization", "")
            if authorization.removeprefix("Bearer ") not in self.accepted_tokens:
                return FakeResponse(401, text="unauthorized")
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404, text="not found")
        if isinstance(route, list):
            return route.pop(0) if route else FakeResponse(500, text="empty")
        return route


class TestCookieTokenAutoRenew:
    """cookies/token 过期自动获取机制：401 触发恰好一次重新登录后重试成功。"""

    LIST_URL = "https://www.ingmm.com/price/spot.html"

    def test_401_triggers_exactly_one_relogin_then_retry_ok(self):
        provider = FakeAuthProvider(token_start=2)  # 首次登录即产生 tok-2
        session = FakeHttpSession(
            {self.LIST_URL: FakeResponse(200, text=ING_LIST_HTML)},
            accepted_tokens={"tok-2"},  # 预置的旧 token 已失效
        )
        manager = CookieTokenManager(provider, session=session)
        # 预置一份尚未过期的旧凭据（不触发登录）
        manager.prime(Credentials(token="tok-1", cookies={"SESSION": "old"}, expires_at=1_000_000.0))

        first = session.get(self.LIST_URL, headers=manager.headers())
        assert first.status_code == 401
        assert provider.login_count == 0  # 尚未登录

        refreshed = manager.handle_response(first)
        assert refreshed is True
        assert provider.login_count == 1  # 恰好一次重新登录
        assert manager.refresh_count == 1
        assert manager.credentials.token == "tok-2"

        second = session.get(self.LIST_URL, headers=manager.headers())
        assert second.status_code == 200
        assert provider.login_count == 1  # 重试成功，不再重复登录
        assert session.cookies.get("SESSION") == "sess-abc123"

    def test_before_request_refreshes_when_token_near_expiry(self):
        now = [1000.0]
        provider = FakeAuthProvider(clock=lambda: now[0])
        manager = CookieTokenManager(provider, refresh_skew_seconds=60.0, clock=lambda: now[0])
        manager.prime(Credentials(token="tok-1", expires_at=1030.0))

        # 距过期 30s < 60s 提前刷新余量 → 视为过期，before_request 自动重新登录
        assert manager.is_expired() is True
        creds = manager.before_request()
        assert creds.token == "tok-1"
        assert provider.login_count == 1
        assert creds.expires_at == 1000.0 + 3600.0

        # 时间冻结在有效期内（距过期 > 60s）：before_request 不再触发登录
        now[0] = 4500.0
        assert manager.is_expired() is False
        manager.before_request()
        manager.before_request()
        assert provider.login_count == 1

        # 再次进入提前刷新窗口：自动重新登录
        now[0] = 4541.0
        assert manager.is_expired() is True
        manager.before_request()
        assert provider.login_count == 2

    def test_expired_page_marker_triggers_refresh(self):
        provider = FakeAuthProvider()
        session = FakeHttpSession({})
        manager = CookieTokenManager(provider, session=session)
        manager.refresh()
        assert provider.login_count == 1

        response = FakeResponse(200, text="<html>系统提示：登录已过期，请重新登录</html>")
        assert manager.handle_response(response) is True
        assert provider.login_count == 2

        ok = FakeResponse(200, text="<html>正常行情数据</html>")
        assert manager.handle_response(ok) is False
        assert provider.login_count == 2

    def test_refresh_skips_when_credentials_valid(self):
        now = [5000.0]
        provider = FakeAuthProvider(clock=lambda: now[0])
        manager = CookieTokenManager(provider, clock=lambda: now[0])
        manager.prime(Credentials(token="tok-1", expires_at=10_000.0))
        assert manager.is_expired() is False
        assert manager.before_request().token == "tok-1"
        assert provider.login_count == 0


class TestEmailNotifier:
    """邮件方式传输登录二维码、验证码与告警（离线 DryRun / Recording）。"""

    def test_dry_run_writes_qr_and_captcha_payloads_to_outbox(self, tmp_path: Path):
        outbox = tmp_path / "outbox"
        notifier = EmailNotifier(DryRunTransport(outbox))

        qr = notifier.send_login_qr(PNG_BYTES, filename="login_qr.png")
        captcha = notifier.send_captcha("482913")
        alert = notifier.send_alert("ing_spot_price 采集失败", "HTTP 500 on list page")

        assert qr.kind == "login_qr"
        assert qr.attachments == (("login_qr.png", PNG_BYTES),)
        assert "482913" in captcha.body
        assert alert.kind == "alert"

        files = sorted(p.name for p in outbox.iterdir())
        assert any(name.endswith("login_qr.eml") for name in files)
        assert any(name.endswith("captcha.eml") for name in files)
        assert any(name.endswith("alert.eml") for name in files)

        qr_attachment = outbox / "001_login_qr__login_qr.png"
        assert qr_attachment.read_bytes() == PNG_BYTES
        qr_eml = (outbox / "001_login_qr.eml").read_text(encoding="utf-8")
        assert "login_qr.png" in qr_eml
        assert "扫码" in qr_eml

        captcha_eml = (outbox / "002_captcha.eml").read_text(encoding="utf-8")
        assert "482913" in captcha_eml
        assert "482913" in captcha.subject
        assert "482913" in captcha.body

        assert alert.subject.startswith("[金融爬虫] 告警")
        assert "HTTP 500" in alert.body

    def test_recording_transport_captures_messages(self):
        transport = RecordingTransport()
        notifier = EmailNotifier(transport, default_recipient="ops@example.com")

        qr = notifier.send_login_qr(PNG_BYTES)
        captcha = notifier.send_captcha("730218")

        assert [m.kind for m in transport.messages] == ["login_qr", "captcha"]
        assert qr.attachments == (("login_qr.png", PNG_BYTES),)
        assert qr.recipient == "ops@example.com"
        assert "login_qr.png" in qr.subject and "login_qr.png" in qr.body
        assert "730218" in captcha.subject and "730218" in captcha.body
        assert notifier.sent == transport.messages

    def test_auth_provider_relays_qr_and_captcha_via_email_on_login(self):
        transport = RecordingTransport()
        notifier = EmailNotifier(transport)
        provider = FakeAuthProvider(
            notifier=notifier,
            qr_png=PNG_BYTES,
            captcha_code="135790",
        )
        manager = CookieTokenManager(provider)

        manager.refresh()

        assert provider.login_count == 1
        assert provider.qr_sent == 1 and provider.captcha_sent == 1
        kinds = [m.kind for m in transport.messages]
        assert kinds == ["login_qr", "captcha"]
        assert transport.messages[0].attachments[0][1] == PNG_BYTES
        assert "135790" in transport.messages[1].subject


class TestAlertingAndRemoteRecovery:
    """告警机制（5 分钟 SLA）与远程恢复命令下发。"""

    def test_on_failure_records_alert_immediately_and_notifies(self):
        transport = RecordingTransport()
        notifier = EmailNotifier(transport)
        now = [1715_000_000.0]
        manager = AlertManager(notifier=notifier, clock=lambda: now[0])

        alert = manager.on_failure("ing_spot_price", RuntimeError("HTTP 502 Bad Gateway"))

        assert isinstance(alert, Alert)
        assert alert.source == "ing_spot_price"
        assert alert.created_at == now[0]  # 立即创建
        assert "HTTP 502" in alert.error
        assert len(transport.messages) == 1  # 同步邮件告警
        message = transport.messages[0]
        assert message.kind == "alert"
        assert "ing_spot_price" in message.subject
        assert "HTTP 502" in message.body
        assert manager.is_within_sla(alert) is True
        assert ALERT_SLA_SECONDS == 300.0

    def test_alert_outside_sla_is_flagged(self):
        now = [1000.0]
        manager = AlertManager(clock=lambda: now[0])
        alert = manager.on_failure("ceci_coal_index", "timeout")
        assert manager.is_within_sla(alert) is True
        now[0] += ALERT_SLA_SECONDS + 1
        assert manager.is_within_sla(alert) is False

    def test_remote_recover_dispatches_captured_command(self):
        transport = RecordingRecoveryTransport()
        calls: list[RecoveryCommand] = []
        manager = AlertManager(recovery_transport=transport)

        manager.register_recovery_handler(
            "ing_spot_price", lambda command: calls.append(command) or True
        )
        command = manager.remote_recover("ing_spot_price", action="restart")

        assert command.action == "restart"
        assert command.source == "ing_spot_price"
        assert transport.commands == [command]
        assert calls == [command]  # 本地处理器同步触发
        assert manager.recoveries == [command]

    def test_wildcard_recovery_handler(self):
        manager = AlertManager()
        calls: list[str] = []
        manager.register_recovery_handler(
            "*", lambda command: calls.append(command.source) or True
        )
        manager.remote_recover("market_price_daily", action="refresh_auth")
        manager.remote_recover("ceci_coal_index")
        assert calls == ["market_price_daily", "ceci_coal_index"]

    def test_on_failure_without_notifier_still_records(self):
        manager = AlertManager()
        alert = manager.on_failure("x", "boom")
        assert manager.alerts == [alert]


# --------------------------------------------------------------------------- 离线爬虫栈


def make_ing_routes() -> dict[str, Any]:
    return {
        IngSpotPriceCrawler.SOURCE.base_url + IngSpotPriceCrawler.SOURCE.list_path: FakeResponse(
            200, text=ING_LIST_HTML
        ),
        "https://www.ingmm.com/price/detail/cu1.html": FakeResponse(
            200, text=ING_DETAIL_CU1_HTML
        ),
        "https://www.ingmm.com/price/detail/al00.html": FakeResponse(
            200, text=ING_DETAIL_AL00_HTML
        ),
    }


def make_ceci_routes() -> dict[str, Any]:
    base = CeciCoalIndexCrawler.SOURCE.base_url
    return {
        base + CeciCoalIndexCrawler.SOURCE.list_path: FakeResponse(
            200, payload=CECI_LIST_JSON
        ),
        base + "/ceci/api/detail/ceci-5500k.json": FakeResponse(
            200, payload=CECI_DETAIL_5500K_JSON
        ),
        base + "/ceci/api/detail/ceci-4700k.json": FakeResponse(
            200, payload=CECI_DETAIL_4700K_JSON
        ),
    }


def make_market_routes() -> dict[str, Any]:
    base = MarketPriceDailyCrawler.SOURCE.base_url
    return {
        base + MarketPriceDailyCrawler.SOURCE.list_path: FakeResponse(
            200, text=MARKET_LIST_HTML
        ),
        base + "/market/daily/2026-05-12.html": FakeResponse(200, text=MARKET_DETAIL_HTML),
    }


ROUTE_BUILDERS = {
    IngSpotPriceCrawler: make_ing_routes,
    CeciCoalIndexCrawler: make_ceci_routes,
    MarketPriceDailyCrawler: make_market_routes,
}


def make_crawler(crawler_cls, tmp_path: Path, *, routes=None, recovery=None):
    """构造一个完全离线的爬虫栈（Fake 会话 + 记录型邮件/恢复传输 + sqlite）。"""
    session = FakeHttpSession(routes if routes is not None else ROUTE_BUILDERS[crawler_cls]())
    provider = FakeAuthProvider()
    auth = CookieTokenManager(provider, session=session, source=crawler_cls.SOURCE.name)
    email_transport = RecordingTransport()
    notifier = EmailNotifier(email_transport)
    recovery_transport = RecordingRecoveryTransport()
    manager = AlertManager(
        notifier=notifier,
        recovery_transport=recovery_transport,
    )
    if recovery is not None:
        manager.register_recovery_handler(crawler_cls.SOURCE.name, recovery)
    writer = SqliteWriter(str(tmp_path / "finance.db"))
    crawler = crawler_cls(
        session=session,
        auth=auth,
        notifier=notifier,
        writer=writer,
        alert_manager=manager,
    )
    return {
        "crawler": crawler,
        "session": session,
        "provider": provider,
        "auth": auth,
        "notifier": notifier,
        "email_transport": email_transport,
        "recovery_transport": recovery_transport,
        "manager": manager,
        "writer": writer,
    }


class TestCrawlersEndToEnd:
    """三个爬虫：解析夹具数据并端到端离线落库（sqlite）。"""

    def test_ing_spot_price_crawler(self, tmp_path: Path):
        stack = make_crawler(IngSpotPriceCrawler, tmp_path)
        crawler: BaseFinanceCrawler = stack["crawler"]

        rows = crawler.collect()
        assert len(rows) == 2
        assert rows[0] == {
            "index_name": "1#电解铜",
            "price": 78650.0,
            "unit": "元/吨",
            "ts": "2026-05-12 10:30",
        }
        assert rows[1]["index_name"] == "A00铝"
        assert rows[1]["price"] == 20850.0

        assert crawler.run() == 2
        writer: SqliteWriter = stack["writer"]
        assert writer.count("ing_spot_price") == 2
        stored = writer.fetch("ing_spot_price")
        assert stored[0]["index_name"] == "1#电解铜"
        assert stored[1]["price"] == 20850.0

    def test_ceci_coal_index_crawler(self, tmp_path: Path):
        stack = make_crawler(CeciCoalIndexCrawler, tmp_path)
        crawler = stack["crawler"]

        rows = crawler.collect()
        assert rows == [
            {"name": "CECI进口煤5500K", "value": 812.5, "ts": "2026-05-11", "change": -3.2},
            {"name": "CECI进口煤4700K", "value": 688.0, "ts": "2026-05-11", "change": 1.5},
        ]

        assert crawler.run() == 2
        writer: SqliteWriter = stack["writer"]
        assert writer.count("ceci_coal_index") == 2
        assert writer.fetch("ceci_coal_index")[0]["value"] == 812.5

    def test_market_price_daily_crawler(self, tmp_path: Path):
        stack = make_crawler(MarketPriceDailyCrawler, tmp_path)
        crawler = stack["crawler"]

        rows = crawler.collect()
        assert rows == [
            {"region": "甘肃", "price": 0.3124, "ts": "2026-05-12"},
            {"region": "青海", "price": 0.2987, "ts": "2026-05-12"},
        ]

        assert crawler.run() == 2
        writer: SqliteWriter = stack["writer"]
        assert writer.count("market_price_daily") == 2
        regions = [row["region"] for row in writer.fetch("market_price_daily")]
        assert regions == ["甘肃", "青海"]


class TestCrawlerAuthAndRecovery:
    """爬虫侧的 401 自动重登重试、失败告警与远程恢复后重试成功。"""

    def test_crawler_auto_relogin_on_401(self, tmp_path: Path):
        list_url = IngSpotPriceCrawler.SOURCE.base_url + IngSpotPriceCrawler.SOURCE.list_path
        routes = {
            # 第一次请求返回 401，第二次才返回列表页
            list_url: [FakeResponse(401, text="unauthorized"), FakeResponse(200, text=ING_LIST_HTML)],
            "https://www.ingmm.com/price/detail/cu1.html": FakeResponse(200, text=ING_DETAIL_CU1_HTML),
            "https://www.ingmm.com/price/detail/al00.html": FakeResponse(200, text=ING_DETAIL_AL00_HTML),
        }
        stack = make_crawler(IngSpotPriceCrawler, tmp_path, routes=routes)
        session: FakeHttpSession = stack["session"]
        session.accepted_tokens = {"tok-1", "tok-2"}
        # 预置有效凭据：首次请求不再触发登录，401 后的重新登录才是"自动重登"
        stack["auth"].prime(
            Credentials(
                token="tok-1",
                cookies={"SESSION": "sess-abc123"},
                expires_at=9_999_999_999.0,
            )
        )
        crawler = stack["crawler"]

        assert crawler.run() == 2
        assert stack["provider"].login_count == 1  # 恰好一次自动重新登录
        assert stack["auth"].refresh_count == 1
        assert stack["writer"].count("ing_spot_price") == 2

    def test_failure_alerts_then_remote_recovery_retry_succeeds(self, tmp_path: Path):
        list_url = CeciCoalIndexCrawler.SOURCE.base_url + CeciCoalIndexCrawler.SOURCE.list_path
        routes = {list_url: FakeResponse(503, text="service unavailable")}
        stack = make_crawler(CeciCoalIndexCrawler, tmp_path, routes=routes)

        # 恢复处理器：模拟远程重启后把路由换成正常夹具数据
        def recover(command: RecoveryCommand) -> bool:
            assert command.action == "restart"
            stack["session"].routes.update(make_ceci_routes())
            return True

        stack = make_crawler(
            CeciCoalIndexCrawler,
            tmp_path,
            routes=routes,
            recovery=recover,
        )
        crawler = stack["crawler"]

        assert crawler.run() == 2  # 恢复后重试成功
        manager: AlertManager = stack["manager"]
        assert len(manager.alerts) == 1  # 恰好一条告警
        assert "503" in manager.alerts[0].error
        assert manager.alerts[0].created_at > 0
        assert manager.is_within_sla(manager.alerts[0])
        assert len(manager.recoveries) == 1
        assert manager.recoveries[0].action == "restart"
        assert stack["recovery_transport"].commands == manager.recoveries
        alert_messages = [m for m in stack["email_transport"].messages if m.kind == "alert"]
        assert len(alert_messages) == 1
        assert "ceci_coal_index" in alert_messages[0].subject
        assert stack["writer"].count("ceci_coal_index") == 2

    def test_failure_without_alert_manager_raises(self, tmp_path: Path):
        list_url = MarketPriceDailyCrawler.SOURCE.base_url + MarketPriceDailyCrawler.SOURCE.list_path
        session = FakeHttpSession({list_url: FakeResponse(500, text="boom")})
        crawler = MarketPriceDailyCrawler(
            session=session,
            auth=CookieTokenManager(FakeAuthProvider(), session=session),
        )
        with pytest.raises(FinanceCrawlError):
            crawler.run()

    def test_mysql_writer_stub_records_offline(self):
        writer = MySqlWriter()
        rows = [{"region": "甘肃", "price": 0.3124, "ts": "2026-05-12"}]
        assert writer.write("market_price_daily", "market_prices", rows) == 1
        assert writer.written == [("market_price_daily", "market_prices", rows[0])]


class TestRegistry:
    """注册表：三个来源登记、元数据与工厂函数。"""

    def test_registry_has_three_sources(self):
        assert set(FINANCE_SOURCES) == {"ing_spot_price", "ceci_coal_index", "market_price_daily"}
        assert set(available_sources()) == set(FINANCE_SOURCES)
        for name, meta in FINANCE_SOURCES.items():
            assert meta.name == name
            assert meta.category
            assert meta.interval_seconds > 0
            assert meta.base_url.startswith("https://")

    def test_get_meta_and_build_crawler(self, tmp_path: Path):
        meta = get_meta("ing_spot_price")
        assert meta is not None
        assert meta.crawler_cls is IngSpotPriceCrawler
        assert get_meta("__nope__") is None

        stack = make_crawler(IngSpotPriceCrawler, tmp_path)
        built = build_crawler(
            "ing_spot_price",
            session=stack["session"],
            provider=stack["provider"],
            auth=stack["auth"],
        )
        assert isinstance(built, IngSpotPriceCrawler)

    def test_build_all(self, tmp_path: Path):
        session = FakeHttpSession({**make_ing_routes(), **make_ceci_routes(), **make_market_routes()})
        provider = FakeAuthProvider()
        crawlers = build_all(session=session, provider=provider)
        assert set(crawlers) == set(FINANCE_SOURCES)
        for name, crawler in crawlers.items():
            assert isinstance(crawler, FINANCE_SOURCES[name].crawler_cls)
            assert crawler.auth is not None

