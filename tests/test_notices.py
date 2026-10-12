"""公告栏服务端预生成的回归测试（全离线，不连库、不联网）。

覆盖的都是**真跑才暴露**的坑，每条都写清当时的现象。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from newsplatform.services.news_search import SearchDoc, _site_of
from newsplatform.services.notice_service import (
    NoticeBundle,
    NoticeService,
    _is_daily_quota_error,
    _tomorrow_midnight,
)

# ======================================================================
# 1. 日额度识别：决定「要不要暂停刷新」
# ======================================================================


def test_detects_openrouter_daily_quota_message():
    """实测原文：Rate limit exceeded: free-models-per-day.

    认不出来就会按原间隔（2 小时）空转重试，白打请求并把日志刷满错误 ——
    而这当天根本不会恢复。
    """
    msg = (
        'HTTP 429：{"error":{"message":"Rate limit exceeded: free-models-per-day. '
        'Add 10 credits to unlock 1000 free model requests per day"}}'
    )
    assert _is_daily_quota_error(RuntimeError(msg)) is True


@pytest.mark.parametrize("text", [
    "daily limit exceeded",
    "you have hit your per_day quota",
    "Daily quota reached",
])
def test_daily_quota_variants(text):
    assert _is_daily_quota_error(RuntimeError(text)) is True


def test_minute_or_transient_errors_are_not_daily_quota():
    """分钟级限流与网络抖动**不应**触发「暂停到明天」—— 那会让服务白停一天。"""
    assert _is_daily_quota_error(RuntimeError("HTTP 429: Rate limit exceeded: free-models-per-minute")) is False
    assert _is_daily_quota_error(RuntimeError("网络错误: connection reset")) is False
    assert _is_daily_quota_error(RuntimeError("HTTP 500 Internal Server Error")) is False


def test_pause_targets_next_day():
    target = _tomorrow_midnight()
    assert target.date() == (datetime.now() + timedelta(days=1)).date()
    assert (target.hour, target.minute) == (0, 5)


# ======================================================================
# 2. 降级结果不覆盖有价值的缓存
# ======================================================================


def _bundle(method: str, points: int) -> NoticeBundle:
    return NoticeBundle(
        headline="h", key_points=[{"text": f"p{i}"} for i in range(points)],
        highlights=[], notices=[{"text": f"n{i}"} for i in range(points)],
        generated_at="2026-10-12 12:00:00", method=method,
        sources={}, caveats=[],
    )


@pytest.mark.asyncio
async def test_degraded_result_does_not_overwrite_llm_cache(monkeypatch):
    """回归：第 2 次刷新遇 429 降级成「标题罗列」，把上一轮成功的
    「8 要点 + 5 亮点」覆盖掉了 —— 公告栏无理由地变差。

    正确行为：保留旧的成功结果，只更新下次刷新时刻。
    """
    svc = NoticeService()
    svc._cache = _bundle("llm", 8)

    async def fake_generate(reason):  # noqa: ANN001, ANN202
        return _bundle("title-only", 3)

    async def noop_save(bundle):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(svc, "_generate", fake_generate)
    monkeypatch.setattr(svc, "_save_to_redis", noop_save)

    result = await svc.refresh(reason="schedule")

    assert result.method == "llm", "降级结果不该覆盖 AI 缓存"
    assert len(result.key_points) == 8, "要点数不该被降级结果改写"


@pytest.mark.asyncio
async def test_degraded_result_is_used_when_no_cache_exists(monkeypatch):
    """首次启动且 LLM 不可用时，降级结果要**采纳** ——
    有「标题列表 + 说明」好过空公告栏。"""
    svc = NoticeService()

    async def fake_generate(reason):  # noqa: ANN001, ANN202
        return _bundle("title-only", 3)

    async def noop_save(bundle):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(svc, "_generate", fake_generate)
    monkeypatch.setattr(svc, "_save_to_redis", noop_save)

    result = await svc.refresh(reason="startup")
    assert result.method == "title-only"
    assert len(result.notices) == 3


@pytest.mark.asyncio
async def test_successful_result_overwrites_degraded_cache(monkeypatch):
    """反向：AI 成功后要能覆盖掉之前的降级缓存，否则永远升不回去。"""
    svc = NoticeService()
    svc._cache = _bundle("title-only", 3)

    async def fake_generate(reason):  # noqa: ANN001, ANN202
        return _bundle("llm", 6)

    async def noop_save(bundle):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(svc, "_generate", fake_generate)
    monkeypatch.setattr(svc, "_save_to_redis", noop_save)

    result = await svc.refresh(reason="schedule")
    assert result.method == "llm"
    assert len(result.key_points) == 6


# ======================================================================
# 3. 响应契约
# ======================================================================


@pytest.mark.asyncio
async def test_get_reports_not_ready_before_first_generation():
    """预热未完成是**正常中间态**：前端据此提示「生成中」而不是报错。"""
    svc = NoticeService()

    async def no_cache():  # noqa: ANN202
        return None

    svc._load_from_redis = no_cache  # type: ignore[method-assign]
    data = await svc.get()
    assert data["ready"] is False
    assert "启动" in data["reason"]
    assert data["notices"] == []


def test_status_exposes_scheduler_and_quota_fields():
    status = NoticeService().status()
    for key in ("cached", "interval_seconds", "scheduler_running", "llm_configured", "paused_until"):
        assert key in status, f"status 缺少 {key}"


def test_bundle_roundtrips_through_dict():
    """缓存要经 JSON 往返（Redis），字段不能丢。"""
    original = _bundle("llm", 2)
    original.next_refresh_at = "2026-10-12 14:00:00"
    restored = NoticeBundle.from_dict(original.to_dict())
    assert restored.method == "llm"
    assert len(restored.key_points) == 2
    assert restored.next_refresh_at == "2026-10-12 14:00:00"


# ======================================================================
# 4. 要点清洗：防模型编造引用
# ======================================================================


def test_clean_points_drops_hallucinated_source_keys():
    known = {
        "a1": SearchDoc("a1", "t1", "中国电力新闻网", "u1", "e1"),
        "s2": SearchDoc("s2", "t2", "北极星电力网", "u2", "e2"),
    }
    cleaned = NoticeService._clean_points(  # noqa: SLF001
        [{"text": "有效要点", "source_keys": ["a1", "XX999", "s2", "a1"]}], known, 8,
    )
    assert cleaned[0]["source_keys"] == ["a1", "s2"]
    assert cleaned[0]["reasons"] == []


def test_clean_points_dedupes_sources():
    """回归：同一条要点引用同站点的多篇稿，不去重会显示
    「来源：[中国电力新闻网, 中国电力新闻网]」。"""
    known = {
        "a1": SearchDoc("a1", "t1", "中国电力新闻网", "u1", "e1"),
        "a2": SearchDoc("a2", "t2", "中国电力新闻网", "u2", "e2"),
    }
    cleaned = NoticeService._clean_points(  # noqa: SLF001
        [{"text": "x", "source_keys": ["a1", "a2"]}], known, 8,
    )
    assert cleaned[0]["sources"] == ["中国电力新闻网"]
    assert cleaned[0]["urls"] == ["u1", "u2"], "URL 不该去重（是不同页面）"


def test_clean_points_marks_points_without_valid_keys():
    known = {"a1": SearchDoc("a1", "t", "s", "u", "e")}
    cleaned = NoticeService._clean_points(  # noqa: SLF001
        [{"text": "凭空要点", "source_keys": ["nope"]}], known, 8,
    )
    assert cleaned[0]["source_keys"] == []
    assert cleaned[0]["reasons"] == ["模型未给出有效出处"]


def test_clean_points_respects_cap_as_prefix_not_offset():
    """回归（与前端 ai-llm.js 同源）：``raw[:cap]`` 是**取前 cap 个**，
    写成「从第 cap 个开始」会让 4 条要点 + cap=6 得到空数组。"""
    known = {"a1": SearchDoc("a1", "t", "s", "u", "e")}
    raw = [{"text": f"p{i}", "source_keys": ["a1"]} for i in range(4)]
    assert len(NoticeService._clean_points(raw, known, 6)) == 4  # noqa: SLF001
    assert len(NoticeService._clean_points(raw, known, 2)) == 2  # noqa: SLF001


# ======================================================================
# 5. 公告条目与置顶
# ======================================================================


def test_to_notices_pins_highlights_first():
    kps = [{"text": "要点", "sources": [], "urls": [], "reasons": []}]
    hls = [{"text": "亮点", "sources": [], "urls": [], "reasons": []}]
    notices = NoticeService._to_notices(kps, hls)  # noqa: SLF001
    assert [n["kind"] for n in notices] == ["highlight", "keypoint"]
    assert notices[0]["pinned"] is True
    assert notices[1]["pinned"] is False


# ======================================================================
# 6. 搜索结果归一
# ======================================================================


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.cpnn.com.cn/a/b", "cpnn.com.cn"),
    ("http://nea.gov.cn/x", "nea.gov.cn"),
    ("https://sub.news.cn/p", "sub.news.cn"),
])
def test_site_of_strips_www_only(url, expected):
    assert _site_of(url) == expected


def test_extract_items_tolerates_several_envelope_shapes():
    """anysearch 的返回外层形状不固定，写死一种会让结果静默为空。"""
    from newsplatform.services.news_search import _extract_items

    item = {"title": "t", "url": "https://x/y"}
    assert _extract_items([item]) == [item]
    assert _extract_items({"results": [item]}) == [item]
    assert _extract_items({"data": {"items": [item]}}) == [item]
    assert _extract_items({"items": [item]}) == [item]
    assert _extract_items({"unknown": 1}) == []
    assert _extract_items("not a dict") == []
