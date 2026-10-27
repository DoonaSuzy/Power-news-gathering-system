"""站点级采集指标的回归测试。

这些指标存在的意义：412 反爬把站点全拦住时，只有 HTTP 指标是看不出来的——
原来的 `/metrics` 一片绿，而库里的文章已经 10 小时没动。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from prometheus_client import REGISTRY

from newsplatform.services import site_metrics as sm


def _value(name: str, **labels) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


@pytest.fixture(autouse=True)
def _reset():
    """每个用例前清空，避免用例之间互相污染。"""
    for gauge in (
        sm.SOURCE_ENABLED,
        sm.SOURCE_LAST_SUCCESS_TS,
        sm.SOURCE_LAST_PUBLISHED_TS,
        sm.SOURCE_ARTICLES_24H,
        sm.SOURCE_FAILURES_24H,
        sm.SOURCE_LAST_JOB_FAILED,
    ):
        gauge.clear()
    yield


class TestEpoch:
    def test_none_is_zero(self):
        assert sm._epoch(None) == 0.0

    def test_datetime_converted(self):
        dt = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        assert sm._epoch(dt) == pytest.approx(dt.timestamp())


class TestApplyRows:
    def test_metrics_written(self):
        rows = [
            {
                "source": "bjx",
                "channel": "power",
                "province": "",
                "category": "industry",
                "enabled": True,
                "last_success": datetime(2026, 10, 27, 10, 0, tzinfo=UTC),
                "last_published": datetime(2026, 10, 27, 9, 30, tzinfo=UTC),
                "articles_24h": 12,
                "failures_24h": 0,
                "last_status": "succeeded",
                "global_last_created": datetime(2026, 10, 27, 10, 0, tzinfo=UTC),
            }
        ]
        sm.apply_rows(rows)

        labels = {
            "source": "bjx",
            "channel": "power",
            "province": "",
            "category": "industry",
        }
        assert _value("powernews_source_enabled", **labels) == 1.0
        assert _value("powernews_source_articles_24h", **labels) == 12.0
        assert _value("powernews_source_failures_24h", **labels) == 0.0
        assert _value("powernews_source_last_job_failed", **labels) == 0.0

    def test_failed_status_flag(self):
        sm.apply_rows(
            [
                {
                    "source": "hb_sgcc",
                    "channel": "power",
                    "province": "130000",
                    "category": "sgcc",
                    "enabled": True,
                    "last_status": "retry_wait",
                    "failures_24h": 4,
                }
            ]
        )
        labels = {
            "source": "hb_sgcc",
            "channel": "power",
            "province": "130000",
            "category": "sgcc",
        }
        assert _value("powernews_source_last_job_failed", **labels) == 1.0
        assert _value("powernews_source_failures_24h", **labels) == 4.0

    def test_disabled_source(self):
        sm.apply_rows(
            [{"source": "csg_hq", "channel": "power", "province": "", "category": "csg",
              "enabled": False}]
        )
        assert _value(
            "powernews_source_enabled",
            source="csg_hq", channel="power", province="", category="csg",
        ) == 0.0

    def test_stale_labels_cleared(self):
        """站点被删后，旧标签不能一直挂在指标里。"""
        sm.apply_rows([{"source": "old", "channel": "power", "province": "", "category": ""}])
        assert _value(
            "powernews_source_enabled", source="old", channel="power", province="", category=""
        ) == 0.0
        sm.apply_rows([])
        assert _value(
            "powernews_source_enabled", source="old", channel="power", province="", category=""
        ) is None

    def test_returns_latest_created(self):
        dt = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)
        got = sm.apply_rows(
            [
                {"source": "a", "channel": "power", "province": "", "category": "",
                 "global_last_created": dt},
                {"source": "b", "channel": "power", "province": "", "category": "",
                 "global_last_created": datetime(2026, 10, 27, 9, 0, tzinfo=UTC)},
            ]
        )
        assert got == pytest.approx(dt.timestamp())
