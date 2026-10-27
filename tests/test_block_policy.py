"""任务终态判定：挑战型阻断（瑞数 412）不该重试。

背景：412 拿到的是同一张 JS 挑战页，重试无用且会加重风控。
实测吉林站点连续重试 3 轮全是 412，期间白白多打了十几次请求。

本用例锁两件事：
1. 412 → `failed`（不重试），且阻断种类被记录；
2. 其它阻断（429 限速）仍按原逻辑重试，不要误伤。
"""

from __future__ import annotations

from datetime import datetime

from newsagg.crawl.session import CrawlSession
from newsagg.timewindow import Window


def _session() -> CrawlSession:
    window = Window(
        start_cst=datetime(2026, 10, 27, 0, 0),
        end_cst=datetime(2026, 10, 27, 12, 0),
        job_type="incremental",
    )
    return CrawlSession(1, source_id=1, window=window, adapter_name="jl_sgcc")


class TestRiverSecurity:
    def test_412_fails_without_retry(self):
        s = _session()
        s.note_block(412)
        status, retry_minutes = s.decide()
        assert status == "failed"
        assert retry_minutes == 0

    def test_block_kind_recorded(self):
        s = _session()
        s.note_block(412)
        assert "river_security" in s.block_kinds

    def test_429_is_rate_limit_not_river(self):
        """限速可恢复，仍按原逻辑重试，不能被误判成挑战型。"""
        s = _session()
        s.note_block(429)
        assert "rate_limit" in s.block_kinds
        assert "river_security" not in s.block_kinds

    def test_403_is_path_or_waf(self):
        s = _session()
        s.note_block(403)
        assert "path_or_waf" in s.block_kinds


class TestDecideUnchanged:
    def test_no_stop_is_success(self):
        s = _session()
        s.listed = 5
        s.saved = 2
        assert s.decide() == ("succeeded", 0)

    def test_site_not_updated_is_success(self):
        """listed>0 但没新文章，是正常（站点未更新）。"""
        s = _session()
        s.listed = 8
        assert s.decide() == ("succeeded", 0)

    def test_empty_list_retries(self):
        """列表页一条都没解析出来，仍按真异常重试。"""
        s = _session()
        status, minutes = s.decide()
        assert status == "retry_wait"
        assert minutes == 120
