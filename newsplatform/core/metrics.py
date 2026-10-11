"""Prometheus 指标与 ASGI 中间件：为 FastAPI 服务接入监控体系。

对应简历「项目二」交付物：``集成 Prometheus + Grafana 搭建监控体系，配置核心指标
看板，实现异常实时告警``；``API 平均响应 ≤ 200ms``。

指标（命名空间 ``power``）：
- ``REQUEST_COUNT``：HTTP 请求总数（method / path / status）。
- ``REQUEST_LATENCY``：请求耗时直方图（method / path，桶围绕 200ms 调优）。
- ``ARTICLES_INGESTED``：文章入库数（source）。
- ``SCHEDULING_SUCCESS``：调度成功率（0..1 仪表）。
- ``QUEUE_DEPTH``：待处理队列深度。
- ``ALERTS_TOTAL``：触发告警总数。

- :class:`PrometheusMiddleware`：ASGI 中间件，包裹每个请求记录计数与耗时。
- :func:`instrument_app`：为应用挂上中间件并注册 ``GET /metrics``（幂等）。
- 辅助函数：``record_article`` / ``set_scheduling_success`` / ``set_queue_depth`` /
  ``inc_alerts``。

导入安全：仅依赖 ``prometheus_client`` 与 ``starlette``（FastAPI 的传递依赖），
可在测试中构建极简 FastAPI 应用而不触碰数据库。
"""

from __future__ import annotations

import re
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "ARTICLES_INGESTED",
    "SCHEDULING_SUCCESS",
    "QUEUE_DEPTH",
    "ALERTS_TOTAL",
    "PrometheusMiddleware",
    "instrument_app",
    "record_article",
    "set_scheduling_success",
    "set_queue_depth",
    "inc_alerts",
    "normalize_path",
    "METRICS_PATH",
]

#: 指标抓取端点路径。
METRICS_PATH = "/metrics"

#: 直方图桶（单位：秒），围绕 200ms SLO 细分，便于计算 p95 与告警阈值 0.2s。
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0)

#: HTTP 请求总数。暴露为 ``power_http_requests_total``。
REQUEST_COUNT = Counter(
    "power_http_requests",
    "HTTP 请求总数。",
    ["method", "path", "status"],
)

#: HTTP 请求耗时直方图。暴露为 ``power_http_request_duration_seconds``。
REQUEST_LATENCY = Histogram(
    "power_http_request_duration_seconds",
    "HTTP 请求耗时（秒）。",
    ["method", "path"],
    buckets=_LATENCY_BUCKETS,
)

#: 成功入库的文章数。暴露为 ``power_articles_ingested_total``。
ARTICLES_INGESTED = Counter(
    "power_articles_ingested",
    "成功入库的文章数。",
    ["source"],
)

#: 调度成功率（0..1）。
SCHEDULING_SUCCESS = Gauge(
    "power_scheduling_success_ratio",
    "调度成功率（0..1）。",
)

#: 待处理队列深度。
QUEUE_DEPTH = Gauge(
    "power_queue_depth",
    "待处理队列深度。",
)

#: 触发告警总数。暴露为 ``power_alerts_total``。
ALERTS_TOTAL = Counter(
    "power_alerts",
    "触发告警总数。",
)

_DIGIT_SEG = re.compile(r"^\d+$")


def normalize_path(path: str) -> str:
    """归一化路径以控制标签基数：纯数字段替换为 ``:id``。

    避免 ``/api/v1/articles/1``、``/2`` …… 造成 Prometheus 标签基数爆炸。
    """
    if not path:
        return path
    segments = path.split("/")
    return "/".join(":id" if _DIGIT_SEG.match(s) else s for s in segments)


class PrometheusMiddleware:
    """ASGI 中间件：记录每个 HTTP 请求的计数与耗时。

    兼容任何 ASGI 应用（FastAPI / Starlette）。``/metrics`` 自身不采集，避免抓取
    动作污染指标。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == METRICS_PATH:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        label_path = normalize_path(path)
        status = 500
        start = time.perf_counter()

        async def send_wrapper(message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            REQUEST_COUNT.labels(method=method, path=label_path, status=status).inc()
            REQUEST_LATENCY.labels(method=method, path=label_path).observe(elapsed)


def instrument_app(app) -> None:
    """为 FastAPI / Starlette 应用接入 Prometheus：挂中间件 + 注册 ``/metrics``。

    幂等：重复调用不会重复挂载中间件或路由。人类接线点：在 ``main.py`` 装配完
    业务路由后调用 ``instrument_app(app)`` 即可。
    """
    if not getattr(app.state, "_prometheus_instrumented", False):
        app.add_middleware(PrometheusMiddleware)
        app.state._prometheus_instrumented = True

    if not any(getattr(route, "path", None) == METRICS_PATH for route in app.routes):
        from starlette.responses import Response

        @app.get(METRICS_PATH, include_in_schema=False)
        async def _metrics_endpoint():  # noqa: ANN001, ANN201
            return Response(
                content=generate_latest(REGISTRY),
                media_type=CONTENT_TYPE_LATEST,
            )


def record_article(source: str) -> None:
    """记录一篇文章入库（按来源计数）。"""
    ARTICLES_INGESTED.labels(source=source or "unknown").inc()


def set_scheduling_success(value: float) -> None:
    """设置调度成功率（裁剪到 0..1）。"""
    SCHEDULING_SUCCESS.set(max(0.0, min(1.0, float(value))))


def set_queue_depth(n: int) -> None:
    """设置待处理队列深度。"""
    QUEUE_DEPTH.set(int(n))


def inc_alerts() -> None:
    """触发告警计数 +1。"""
    ALERTS_TOTAL.inc()

