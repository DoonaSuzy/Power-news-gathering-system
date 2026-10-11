"""Prometheus 指标测试：极简 FastAPI 应用 + instrument_app + TestClient。

验证：
- ASGI 中间件记录 REQUEST_COUNT / REQUEST_LATENCY（含数字路径归一化）。
- GET /metrics 返回 prometheus 文本格式且包含 ``power_`` 指标名。
- 辅助函数 record_article / inc_alerts / gauge 正确更新指标。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from newsplatform.core import metrics


def _build_app() -> FastAPI:
    """构建一个不依赖数据库的极简 FastAPI 应用并接入 Prometheus。"""
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    metrics.instrument_app(app)
    return app


def _sample(name: str, labels: dict | None = None):
    return REGISTRY.get_sample_value(name, labels)


class TestMiddleware:
    def test_request_count_and_latency_recorded(self):
        app = _build_app()
        with TestClient(app) as client:
            assert client.get("/ping").status_code == 200

        got = _sample("power_http_requests_total",
                      {"method": "GET", "path": "/ping", "status": "200"}) or 0
        assert got >= 1
        cnt = _sample("power_http_request_duration_seconds_count",
                      {"method": "GET", "path": "/ping"}) or 0
        assert cnt >= 1

    def test_numeric_path_normalized(self):
        app = _build_app()
        with TestClient(app) as client:
            client.get("/items/42")
        got = _sample("power_http_requests_total",
                      {"method": "GET", "path": "/items/:id", "status": "200"}) or 0
        assert got >= 1


class TestMetricsEndpoint:
    def test_metrics_text_contains_power_metrics(self):
        app = _build_app()
        with TestClient(app) as client:
            client.get("/ping")
            r = client.get("/metrics")
        assert r.status_code == 200
        assert "power_" in r.text
        assert "power_http_requests_total" in r.text

    def test_content_type_is_prometheus_latest(self):
        app = _build_app()
        with TestClient(app) as client:
            r = client.get("/metrics")
        assert metrics.CONTENT_TYPE_LATEST in r.headers.get("content-type", "")

    def test_metrics_endpoint_not_self_instrumented(self):
        app = _build_app()
        with TestClient(app) as client:
            client.get("/metrics")
        got = _sample("power_http_requests_total",
                      {"method": "GET", "path": "/metrics", "status": "200"}) or 0
        assert got == 0


class TestHelpers:
    def test_record_article_bumps_counter(self):
        before = _sample("power_articles_ingested_total", {"source": "sgcc"}) or 0
        metrics.record_article("sgcc")
        after = _sample("power_articles_ingested_total", {"source": "sgcc"}) or 0
        assert after == before + 1

    def test_inc_alerts_bumps_counter(self):
        before = _sample("power_alerts_total") or 0
        metrics.inc_alerts()
        after = _sample("power_alerts_total") or 0
        assert after == before + 1

    def test_gauge_helpers(self):
        metrics.set_scheduling_success(0.87)
        metrics.set_queue_depth(123)
        assert _sample("power_scheduling_success_ratio") == 0.87
        assert _sample("power_queue_depth") == 123

    def test_scheduling_success_clamped(self):
        metrics.set_scheduling_success(5.0)
        assert _sample("power_scheduling_success_ratio") == 1.0
