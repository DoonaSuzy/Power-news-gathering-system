"""错误信封的回归测试：``AppError.extra`` 必须真的到客户端。

背景
----
``core/exceptions.py`` 里每个异常都能带 ``extra``（``to_payload()`` 早就写了），
但 ``exception_handlers.py`` 只下发了 ``code`` 与 ``detail``，``extra`` 被丢掉。
后果不是「少一个字段」这么轻：409 的 ``candidates``（同编号在两个数据域各有一条）
正是靠 ``extra`` 传的，丢掉之后客户端只看到「冲突」，无从知道冲突的是什么，
也就没法带 ``channel`` 重取——报错变成死路。

这条用例把「_app_error 处理器必须透传 extra」钉住；顺带守住错误码表的完整性：
每个 ``AppError`` 子类的 ``code`` 都要在 ``ERROR_CODES`` 里，否则日志与告警里
会出现 ``unknown_5002`` 这种既搜不到也解释不了的名字（真发生过）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from newsplatform.core.exception_handlers import register_exception_handlers
from newsplatform.core.exceptions import (
    ERROR_CODES,
    AmbiguousArticle,
    AppError,
    ChannelUnavailable,
)


def _client_raising(exc: AppError) -> TestClient:
    """装一个只有一条路由的 app，访问即抛指定异常。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:  # pragma: no cover - 只为触发异常
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_extra_reaches_body():
    """``extra`` 里的 candidates 必须出现在响应体顶层。"""
    exc = AmbiguousArticle(
        "编号 2234 在电力与省级两个数据域都存在",
        extra={"article_id": 2234, "candidates": [{"channel": "power", "id": 2234}]},
    )
    resp = _client_raising(exc).get("/boom")

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 4002
    assert body["candidates"] == [{"channel": "power", "id": 2234}]
    assert body["article_id"] == 2234
    # 统一信封的三个老字段不能因为加了 extra 就丢
    assert {"code", "message", "detail", "request_id", "timestamp"} <= set(body)


def test_every_app_error_code_has_stable_name():
    """所有业务异常码都要在 ``ERROR_CODES`` 里有名字。

    当前能立刻抓到 ``ChannelUnavailable.code = 5002``：它曾经不在码表里，
    日志显示成 ``unknown_5002``，把「这个域没接」和「服务端崩了」混成一类。
    """

    def walk(cls: type[AppError]):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    missing = {sub.__name__: sub.code for sub in walk(AppError) if sub.code not in ERROR_CODES}
    assert not missing, f"这些异常码不在 ERROR_CODES 里：{missing}"
    assert ChannelUnavailable.code in ERROR_CODES
    assert "unknown_" not in "".join(ERROR_CODES.values())
