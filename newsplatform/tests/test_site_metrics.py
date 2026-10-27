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
        sm.SOURCE_INTERVAL_HOURS,
    ):
        gauge.clear()
    yield


class TestEpoch:
    def test_none_is_zero(self):
        assert sm._epoch(None) == 0.0

    def test_datetime_converted(self):
        dt = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        assert sm._epoch(dt) == pytest.approx(dt.timestamp())

    def test_naive_datetime_treated_as_utc(self):
        """库里是 naive UTC，缺时区时必须按 UTC 解释。

        按本地时区解释会让全部 *_ts 指标整体偏移（+08:00 就是 8 小时），
        SourceStale / CollectorLagHigh 会集体误判，而且现象是「一直报/一直不报」，
        很难与真实的采集停摆区分。
        """
        naive = datetime(2026, 10, 27, 12, 0)
        aware = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        assert sm._epoch(naive) == pytest.approx(aware.timestamp())


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


class TestIntervalHours:
    """采集间隔指标：告警阈值要按它分档，缺了就只能用固定阈值（必然误报）。"""

    _LABELS = {"source": "bjx", "channel": "power", "province": "", "category": "industry"}

    def test_interval_written(self):
        sm.apply_rows([{**self._LABELS, "interval_hours": 4}])
        assert _value("powernews_source_interval_hours", **self._LABELS) == 4.0

    def test_interval_defaults_to_24(self):
        """缺列/缺值按 24h 保守处理：宁可晚报警，也不要把阈值算成 0。"""
        sm.apply_rows([{**self._LABELS}])
        assert _value("powernews_source_interval_hours", **self._LABELS) == 24.0
