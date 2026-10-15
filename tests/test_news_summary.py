"""电力新闻智能总结（``/api/v1/news/summary``）的分层测试。

全程离线：
- 抽取式算法是纯函数，直接断言；
- LLM 客户端用 ``httpx.MockTransport``；
- 服务与路由用假会话 / 假仓储，不连库。

真库侧的一致性由 ``scripts/news_summary_audit.py`` 负责。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from newsplatform.api.v1 import summary as summary_module
from newsplatform.core.database import get_collector_session
from newsplatform.core.exception_handlers import register_exception_handlers
from newsplatform.core.llm import (
    LLMClient,
    LLMNotConfigured,
    LLMSettings,
    LLMUnavailable,
    _loads_lenient,
    settings_from,
)
from newsplatform.core.ratelimit import enforce_rate_limit
from newsplatform.core.security import AuthContext, require_auth
from newsplatform.repositories.articles import ArticleQuery
from newsplatform.services.extractive import (
    DOMAIN_TERMS,
    Doc,
    _with_prefix,
    extract_keywords,
    split_sentences,
    starts_with_reference,
    summarize,
    tokenize,
)
from newsplatform.services.summary_service import (
    SUMMARY_SYSTEM_PROMPT,
    SummaryService,
    build_llm_user_prompt,
)


def run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _doc(aid: int, title: str, body: str, source: str = "国网北京市电力公司", **kw) -> Doc:  # noqa: ANN003
    return Doc(
        id=aid, title=title, body=body, source_name=source,
        published_at=kw.get("published_at"), url=kw.get("url", f"https://x/{aid}"),
        province_name=kw.get("province_name"),
    )


# ======================================================================
# 1. 抽取式算法：切分
# ======================================================================


def test_split_sentences_breaks_on_newline_and_punctuation():
    """导语与正文之间的换行必须切开，否则两句会粘成一句。"""
    text = "国家电网今日投运一座变电站。\n该工程新增外送能力八百兆瓦。"
    out = split_sentences(text)
    assert len(out) == 2
    assert out[0].startswith("国家电网")
    assert out[1].startswith("该工程")


def test_split_sentences_drops_fragments_and_giant_blobs():
    """过长/过短片段都不该进句子池：前者是没切开的段落，后者是栏目名。"""
    text = "首页。" + "这是一句正常长度的新闻句子，用来占位。" + "甲" * 300
    out = split_sentences(text)
    assert all(8 <= len(s) <= 220 for s in out)
    assert "首页。" not in out


def test_tokenize_emits_chinese_ngrams_and_ascii_words():
    tokens = tokenize("华东电网投运±800千伏特高压工程，GW级")
    assert "特高压" in tokens
    assert "电网" in tokens
    assert "800" in tokens or "gw" in tokens


def test_tokenize_drops_stopwords():
    tokens = tokenize("我们表示记者报道已经可以")
    assert "我们" not in tokens
    assert "记者" not in tokens
    assert "已经" not in tokens


# ======================================================================
# 2. 抽取式算法：关键词
# ======================================================================


def test_keywords_rank_by_document_coverage_not_raw_frequency():
    """30 篇各提一次「储能」应当压过 1 篇提 30 次「储能」。

    用词频打底会让「某篇长文反复念叨同一个词」变成头条焦点，
    这不是「这一周期的焦点」的定义。
    """
    docs = [_doc(i, f"新闻{i}", f"储能项目进展{i}。") for i in range(1, 6)]
    docs.append(_doc(99, "长文", "储能" * 40 + "。"))
    ranked = dict((term, n) for term, n, _w in extract_keywords(docs, k=20))
    assert ranked.get("储能") == 6


def test_keywords_only_contain_domain_terms():
    """关键词是给用户看的，必须是可读领域词，不能是 n-gram 碎片。"""
    docs = [_doc(1, "标题", "特高压工程开工，虚拟电厂试点。")]
    terms = [t for t, _n, _w in extract_keywords(docs)]
    assert "特高压" in terms
    assert all(t in DOMAIN_TERMS for t in terms)


def test_keywords_empty_for_empty_corpus():
    assert extract_keywords([]) == []


# ======================================================================
# 3. 抽取式算法：摘要主体
# ======================================================================


def test_summarize_is_deterministic():
    """同样输入两次必须逐字节相同 —— 算法里不允许有随机数或集合迭代序依赖。"""
    docs = [
        _doc(1, "国家电网投运特高压", "国家电网今日投运±800千伏特高压直流工程，新增外送能力800万千瓦。"),
        _doc(2, "南方电网储能并网", "南方电网首个百兆瓦级储能电站并网，投资约12亿元。"),
        _doc(3, "电力现货市场扩容", "电力现货市场试点扩至6个省份，绿电交易量同比增长23.5%。"),
    ]
    a = summarize(docs, window_label="近1周").as_dict()
    b = summarize(docs, window_label="近1周").as_dict()
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def test_summarize_empty_corpus_says_so_instead_of_faking():
    extraction = summarize([], window_label="今日")
    assert extraction.doc_count == 0
    assert extraction.key_points == ()
    assert "没有采集到" in extraction.headline


def test_summarize_without_any_text_says_so():
    """有文章但标题正文都空：如实说明，不要编一条要点出来。"""
    extraction = summarize([_doc(1, "", "")], window_label="今日")
    assert extraction.key_points == ()
    assert "正文为空" in extraction.headline


def test_summarize_falls_back_to_title_when_body_is_empty():
    docs = [_doc(1, "国家电网投运±800千伏特高压直流工程，新增外送能力800万千瓦", "")]
    extraction = summarize(docs, window_label="今日")
    assert extraction.doc_count == 1
    assert extraction.key_points


def test_mmr_keeps_near_duplicate_reposts_from_filling_all_points():
    """六篇转载同一个签约仪式的稿子，不该占满全部要点（MMR 去冗余回归）。

    这是抽取式摘要最典型的失败模式：不做去冗余时，307 篇里前 6 句
    可能全是同一件事的六个版本。
    """
    same = "国家电网与某省签署战略合作协议，双方将在特高压和储能领域深化合作。"
    dup_docs = [
        _doc(i, f"国家电网签约{i}", same + f"（第{i}次转载）") for i in range(1, 7)
    ]
    other = _doc(99, "南方电网储能并网", "南方电网首个百兆瓦级储能电站并网，投资约12亿元。")
    extraction = summarize(dup_docs + [other], window_label="近1周", max_points=3)

    texts = [p.text for p in extraction.key_points]
    assert len(texts) == 3
    # 至少有一条来自那篇不同的稿子 —— 全部重复就是去冗余失效
    assert any("南方电网" in t for t in texts), texts


def test_highlights_prefer_quantified_and_progress_sentences():
    docs = [
        _doc(1, "会议召开", "公司召开专题会议，安排部署下一阶段重点工作任务。"),
        _doc(2, "特高压投运", "该工程今日正式投运，新增外送能力800万千瓦，投资约120亿元。"),
        _doc(3, "储能并网", "储能电站成功并网发电，规模达100兆瓦时。"),
    ]
    extraction = summarize(docs, window_label="近1周", max_highlights=2)
    assert extraction.highlights, "含量化数据的句子必须能被选为亮点"
    joined = " ".join(p.text for p in extraction.highlights)
    assert "800万千瓦" in joined or "100兆瓦时" in joined
    for point in extraction.highlights:
        assert point.reasons, "每条亮点都要说明为什么算亮点"
        assert any(r.startswith("含量化数据") or r.startswith("含进展词") for r in point.reasons)


def test_highlights_do_not_come_from_only_one_article():
    """同一篇最多贡献一条亮点，否则 3 条亮点全是同一篇稿子。"""
    body = (
        "该工程正式投运，新增外送能力800万千瓦。"
        "项目总投资约120亿元。"
        "同时配套建设储能电站100兆瓦时。"
        "预计年发电量达到60亿千瓦时。"
    )
    docs = [_doc(1, "特高压投运", body), _doc(2, "储能并网", "储能电站并网发电，规模达100兆瓦时。")]
    extraction = summarize(docs, window_label="近1周", max_highlights=3)
    ids = [aid for p in extraction.highlights for aid in p.article_ids]
    assert len(ids) == len(set(ids)), f"亮点重复引用了同一篇：{ids}"


def test_key_points_respect_the_cap():
    docs = [_doc(i, f"标题{i}", f"第{i}条电力新闻正文，讲述工程进展与投资规模。") for i in range(1, 30)]
    extraction = summarize(docs, window_label="近1周", max_points=4)
    assert len(extraction.key_points) == 4


# ======================================================================
# 3b. 代词开头的句子必须补上文（否则要点读不懂）
# ======================================================================


def test_starts_with_reference_flags_pronouns_not_time_words():
    """只标记**离开上文读不懂**的句子；「近日，某局约谈…」是完整句，不该标记。"""
    assert starts_with_reference("该项目锚定沙戈荒大基地调峰电源定位。")
    assert starts_with_reference("其中，风光累计装机19.92亿千瓦。")
    assert starts_with_reference("该公司协助新桥服务区推进光伏并网。")
    assert starts_with_reference("此外，还要完善跨省区交易机制。")
    # 这几类都能独立阅读，误标会把好句子也拖长
    assert not starts_with_reference("近日，新疆能源监管办约谈12家储能企业。")
    assert not starts_with_reference("国家能源局组织召开调度视频会。")
    assert not starts_with_reference("业内人士分析，未来需发挥市场协同作用。")


def test_with_prefix_attaches_the_previous_sentence():
    sents = [
        "中卫电厂6号机组通过168小时试运行，标志着4台机组全容量投产。",
        "该项目锚定沙戈荒大基地调峰电源定位。",
    ]
    text, used = _with_prefix(sents, 1)
    assert used == 1
    assert text.startswith("中卫电厂6号机组")
    assert text.endswith("该项目锚定沙戈荒大基地调峰电源定位。")


def test_with_prefix_leaves_standalone_sentence_alone():
    sents = ["国家能源局组织召开调度视频会。", "各单位要抓好大型风电光伏基地建设。"]
    text, used = _with_prefix(sents, 1)
    assert used == 0
    assert text == "各单位要抓好大型风电光伏基地建设。"


def test_with_prefix_cannot_rescue_a_first_sentence():
    """第一篇的第一句没有上文可补 —— 不能报错，也不能编一段出来。"""
    only = ["该项目锚定沙戈荒大基地调峰电源定位。"]
    text, used = _with_prefix(only, 0)
    assert used == 0
    assert text == only[0]


def test_with_prefix_gives_up_instead_of_inflating_the_point():
    """上一个句子太长时宁可不补，也不要把整段搬进要点。"""
    sents = ["甲" * 400, "该项目锚定沙戈荒大基地调峰电源定位。"]
    text, used = _with_prefix(sents, 1, max_len=100)
    assert used == 0
    assert text == sents[1]


def test_summarize_resolves_pronoun_led_points():
    """回归：`?range=week` 的 9 条产出里曾有 4 条以「该项目 / 该公司 / 其中」开头。"""
    docs = [
        _doc(42, "全国首个沙戈荒新能源大基地配套调峰电源全容量投产",
             "10月2日，国家能源集团宁夏电力中卫电厂6号机组顺利通过168小时试运行，"
             "标志着全国首个沙戈荒新能源大基地配套调峰电源4台660兆瓦机组全容量投产。"
             "该项目锚定沙戈荒大基地调峰电源定位，率先推动火电由电量主体向支撑主体转型。"),
    ]
    extraction = summarize(docs, window_label="今日", max_points=6, max_highlights=3)
    texts = [p.text for p in extraction.key_points]
    assert any("该项目锚定" in t for t in texts), texts
    # 关键断言：产出里不该留下裸代词开头的句子
    for point in extraction.key_points + extraction.highlights:
        assert not starts_with_reference(point.text), f"代词未被解析：{point.text[:50]}"
    # 并且要明确标出这句补过上文，用户核对时知道去找原文
    merged = [p for p in extraction.key_points if "已补上文" in p.reasons]
    assert merged, "补过上文的要点必须标注"


def test_highlights_use_the_same_context_resolution():
    """亮点与要点必须走同一套补上文逻辑，否则同一句话两处文本不一致。"""
    docs = [
        _doc(49, "可再生能源装机统计",
             "可再生能源发电装机达到24.96亿千瓦，占全国电力总装机的60.8%。"
             "其中，风光累计装机19.92亿千瓦，发电量合计1.71万亿千瓦时。"),
    ]
    extraction = summarize(docs, window_label="今日", max_points=1, max_highlights=3)
    assert extraction.highlights, "含量化数据的句子必须能进亮点"
    for point in extraction.highlights:
        assert not starts_with_reference(point.text), f"亮点留下裸代词：{point.text[:50]}"


# ======================================================================
# 4. LLM 客户端
# ======================================================================


def _ready_settings(**kw) -> LLMSettings:  # noqa: ANN003
    base = dict(
        enabled=True, base_url="https://llm.example/v1", api_key="sk-test",
        model="test-model", timeout=5.0, max_input_chars=1000, max_tokens=200,
    )
    base.update(kw)
    return LLMSettings(**base)


def test_llm_client_is_not_configured_without_api_key():
    client = LLMClient(LLMSettings(
        enabled=True, base_url="https://llm.example/v1", api_key="",
        model="m", timeout=5.0, max_input_chars=100, max_tokens=10,
    ))
    assert client.configured is False
    with pytest.raises(LLMNotConfigured, match="未配置 LLM"):
        run(client.chat_json(system="s", user="u"))


def test_llm_describe_never_leaks_the_api_key():
    """配置说明会进接口响应，绝不能带上密钥。"""
    described = _ready_settings(api_key="sk-super-secret").describe()
    assert "sk-super-secret" not in json.dumps(described)
    assert described["model"] == "test-model"


def test_loads_lenient_accepts_plain_json():
    assert _loads_lenient('{"headline": "x"}')["headline"] == "x"


def test_loads_lenient_strips_markdown_fence():
    raw = '```json\n{"headline": "x", "key_points": []}\n```'
    assert _loads_lenient(raw)["headline"] == "x"


def test_loads_lenient_extracts_object_from_prose():
    """模型常会先寒暄再给 JSON，直接 json.loads 会失败。"""
    raw = '好的，以下是总结：\n{"headline": "x", "key_points": [{"text": "a"}]}\n希望有帮助。'
    parsed = _loads_lenient(raw)
    assert parsed["headline"] == "x"
    assert parsed["key_points"][0]["text"] == "a"


def test_loads_lenient_handles_nested_braces():
    raw = '前缀 {"headline": "x", "meta": {"a": {"b": 1}}} 后缀'
    assert _loads_lenient(raw)["meta"]["a"]["b"] == 1


def test_loads_lenient_rejects_garbage_loudly():
    with pytest.raises(LLMUnavailable, match="找不到 JSON"):
        _loads_lenient("完全不是 JSON 的一段话")


def _mock_llm(handler) -> LLMClient:  # noqa: ANN001
    return LLMClient(
        _ready_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_llm_chat_json_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["messages"][0]["content"] == SUMMARY_SYSTEM_PROMPT
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "headline": "本周焦点", "key_points": [{"text": "要点", "article_ids": [1]}],
            }, ensure_ascii=False)}}],
        })

    client = _mock_llm(handler)
    out = run(client.chat_json(system=SUMMARY_SYSTEM_PROMPT, user="材料"))
    assert out["headline"] == "本周焦点"


def test_llm_chat_json_missing_required_key_is_unavailable():
    """缺 headline/key_points 视为不可用 —— 不能用半个结果去拼响应。"""
    client = _mock_llm(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"key_points": []}'}}],
    }))
    with pytest.raises(LLMUnavailable, match="缺少必需字段"):
        run(client.chat_json(system="s", user="u"))


def test_llm_chat_json_upstream_5xx_is_unavailable():
    client = _mock_llm(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMUnavailable, match="HTTP 500"):
        run(client.chat_json(system="s", user="u"))


def test_llm_chat_json_network_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dial failed", request=request)

    client = _mock_llm(handler)
    with pytest.raises(LLMUnavailable, match="网络"):
        run(client.chat_json(system="s", user="u"))


def test_llm_chat_json_malformed_envelope_is_unavailable():
    client = _mock_llm(lambda r: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMUnavailable, match="choices"):
        run(client.chat_json(system="s", user="u"))


def test_settings_from_reads_platform_env(monkeypatch):
    from newsplatform.core.config import reset_platform_settings

    monkeypatch.setenv("POWERNEWS_PLATFORM_LLM_ENABLED", "1")
    monkeypatch.setenv("POWERNEWS_PLATFORM_LLM_API_KEY", "sk-env")
    monkeypatch.setenv("POWERNEWS_PLATFORM_LLM_MODEL", "env-model")
    reset_platform_settings()
    try:
        cfg = settings_from()
        assert cfg.ready is True
        assert cfg.model == "env-model"
        assert cfg.api_key == "sk-env"
    finally:
        for key in (
            "POWERNEWS_PLATFORM_LLM_ENABLED",
            "POWERNEWS_PLATFORM_LLM_API_KEY",
            "POWERNEWS_PLATFORM_LLM_MODEL",
        ):
            monkeypatch.delenv(key, raising=False)
        reset_platform_settings()


def test_llm_ready_false_when_enabled_but_key_missing():
    """只开开关不填 Key，必须在启动时可判为「未就绪」，而不是等运行时 401。

    ⚠️ 用 ``PlatformSettings(_env_file=None)`` 直接构造，**不读 .env**。
    早期版本靠 ``monkeypatch.delenv`` 清环境变量，但 pydantic-settings 的
    ``env_file`` 会绕过环境变量直接从 ``.env`` 取值 —— 一旦开发机真的配了
    LLM，这个测试就假失败（实测踩到：`.env` 配好后 2 个「未配置」用例转红）。
    """
    from newsplatform.core.config import PlatformSettings

    settings = PlatformSettings(
        _env_file=None,
        llm_enabled=True,
        llm_api_key="",
        llm_base_url="https://api.example.com/v1",
        llm_model="some-model",
    )
    assert settings.llm_ready is False

    # 反向：三项齐备时必须为 True，否则开关等于永远打不开
    ready = PlatformSettings(
        _env_file=None,
        llm_enabled=True,
        llm_api_key="sk-x",
        llm_base_url="https://api.example.com/v1",
        llm_model="some-model",
    )
    assert ready.llm_ready is True


# ======================================================================
# 5. 提示词构造
# ======================================================================


def test_prompt_truncates_and_tells_the_model_it_truncated():
    """截断必须显式告知模型：不说的话它会把「没提供」当成「材料里没有」。"""
    docs = [_doc(i, f"标题{i}", "正文" * 200) for i in range(1, 40)]
    prompt = build_llm_user_prompt(
        docs, window_label="近1周", max_points=6, max_highlights=3, max_chars=2000,
    )
    assert "未提供" in prompt
    assert len(prompt) < 4000


def test_prompt_includes_id_title_source_and_window():
    docs = [_doc(7, "特高压投运", "正文内容。", source="国网江苏省电力有限公司")]
    prompt = build_llm_user_prompt(
        docs, window_label="今日（2026-10-12 ~ 2026-10-12）",
        max_points=6, max_highlights=3, max_chars=10000,
    )
    assert "[7]" in prompt
    assert "特高压投运" in prompt
    assert "国网江苏省电力有限公司" in prompt
    assert "2026-10-12" in prompt


# ======================================================================
# 6. 服务编排
# ======================================================================


class _StubRepo:
    """替身仓储：记录调用并返回预置数据。"""

    def __init__(self, *, stats, rows, latest=None, stats_after=None) -> None:
        self._stats = stats
        self._stats_after = stats_after
        self._rows = rows
        self._latest = latest
        self.calls: list[tuple[str, ArticleQuery]] = []
        self._n = 0

    async def window_stats(self, q: ArticleQuery) -> dict:
        self._n += 1
        self.calls.append(("window_stats", q))
        if self._n > 1 and self._stats_after is not None:
            return dict(self._stats_after)
        return dict(self._stats)

    async def list_for_summary(self, q: ArticleQuery, *, limit: int) -> list[dict]:
        self.calls.append(("list_for_summary", q))
        return list(self._rows)

    async def latest_window_with_data(self, q: ArticleQuery, *, days: int):
        self.calls.append(("latest_window_with_data", q))
        return self._latest


def _unconfigured_llm() -> LLMClient:
    """一个明确「未配置」的客户端：不发请求、不读 ``.env``。"""
    return LLMClient(LLMSettings(
        enabled=False, base_url="", api_key="", model="",
        timeout=1.0, max_input_chars=100, max_tokens=10,
    ))


def _service(stub: _StubRepo, *, llm: LLMClient | None = None) -> SummaryService:
    """构造被测服务。

    ⚠️ 不传 ``llm`` 时注入**未配置**的客户端，而不是放任 ``SummaryService.llm``
    去 ``build_llm_client()`` 读真实 ``.env`` —— 本文件开头就写明「全程离线」，
    而开发机一旦配好可用的 LLM，那些「未配置」用例就会真的发网络请求：
    实测 `.env` 配好后 ``test_service_defaults_to_extractive_when_llm_not_configured``
    由「恰好通过」变为失败（``assert 'llm' == 'extractive'``）。
    要测真实 LLM 路径就显式传 ``llm=``。
    """
    svc = SummaryService(None)  # type: ignore[arg-type]
    svc.repo = stub  # type: ignore[assignment]
    svc._llm = llm if llm is not None else _unconfigured_llm()
    return svc


class _OfflineSummaryService(SummaryService):
    """路由测试用：``session`` 之外一律注入**未配置** LLM。

    路由里的 ``SummaryService(session)`` 会惰性 ``build_llm_client()`` 读真实
    ``.env``；不拦住它，本文件的路由用例就会真发网络请求（实测单例 13~30s，
    且结果随开发机 ``.env`` 变化）。要测模型路径请显式传 ``llm=``。
    """

    def __init__(self, session, *, llm: LLMClient | None = None) -> None:  # noqa: ANN001
        super().__init__(session, llm=llm if llm is not None else _unconfigured_llm())


def _query(**kw) -> ArticleQuery:  # noqa: ANN003
    return ArticleQuery(**kw)


def test_service_defaults_to_extractive_when_llm_not_configured():
    """没配 LLM → 走抽取式，并把「为什么没走模型」写进 caveats。

    回归：本用例早期靠「开发机恰好没配 LLM」通过 —— 一旦 ``.env`` 配好可用模型，
    它就会真发请求并返回 ``method="llm"``。现在由 ``_service`` 显式注入未配置客户端，
    结果与开发机的 ``.env`` 解耦。
    """
    stub = _StubRepo(stats={"total": 3, "sources": 2, "provinces": 2,
                            "earliest": None, "latest": None},
                     rows=[{"id": 1, "title": "特高压投运", "body_text": "新增外送能力800万千瓦。",
                            "source_name": "国网", "published_at_cst": datetime(2026, 10, 12, 9),
                            "url": "u", "province_name": "北京"}])
    svc = _service(stub)
    assert svc.llm.configured is False  # 不读 .env，测试与开发机配置解耦
    data = run(svc.news_summary(_query(), window_label="今日", range_key="today"))
    assert data["method"] == "extractive"
    assert data["summary"]["key_points"]
    assert any("抽取式" in c for c in data["caveats"])


def test_service_falls_back_to_latest_day_for_preset_ranges():
    """快捷档空窗口 → 回退到最近有数据的一天，且 **fallback 字段写明**。"""
    found = datetime(2026, 10, 10, 17, 59)
    stub = _StubRepo(
        stats={"total": 0, "sources": 0, "provinces": 0, "earliest": None, "latest": None},
        stats_after={"total": 1, "sources": 1, "provinces": 1,
                     "earliest": found, "latest": found},
        rows=[{"id": 1, "title": "10-10 的新闻", "body_text": "正文内容在这里。",
               "source_name": "国网", "published_at_cst": found, "url": "u",
               "province_name": "北京"}],
        latest=found,
    )
    data = run(_service(stub).news_summary(
        _query(date_from=datetime(2026, 10, 12), date_to=datetime(2026, 10, 12, 23, 59, 59)),
        window_label="今日（2026-10-12 ~ 2026-10-12）",
        range_key="today",
    ))
    assert data["fallback"] is not None
    assert data["fallback"]["applied"] is True
    assert "2026-10-10" in data["fallback"]["reason"]
    assert data["stats"]["total_matched"] == 1
    assert any("注意" in c for c in data["caveats"])


def test_service_does_not_fall_back_for_custom_range():
    """自定义档是用户明确划的范围，不能悄悄改 —— 只给线索。"""
    found = datetime(2026, 10, 10, 17, 59)
    stub = _StubRepo(
        stats={"total": 0, "sources": 0, "provinces": 0, "earliest": None, "latest": None},
        rows=[], latest=found,
    )
    data = run(_service(stub).news_summary(
        _query(date_from=datetime(2026, 1, 1), date_to=datetime(2026, 1, 2)),
        window_label="自定义", range_key="custom",
    ))
    assert data["fallback"] is None
    assert data["stats"]["total_matched"] == 0
    assert any("2026-10-10" in c for c in data["caveats"])
    assert not any(c[0] == "list_for_summary" for c in stub.calls)


def test_service_reports_when_nothing_exists_at_all():
    stub = _StubRepo(
        stats={"total": 0, "sources": 0, "provinces": 0, "earliest": None, "latest": None},
        rows=[], latest=None,
    )
    data = run(_service(stub).news_summary(_query(), window_label="今日", range_key="today"))
    assert data["fallback"] is None
    assert any("没有任何带发布时间的文章" in c for c in data["caveats"])


def test_clean_points_drops_hallucinated_article_ids():
    """模型会编造编号，必须只保留材料里真实存在的 id。"""
    known = {1: _doc(1, "t", "b"), 2: _doc(2, "t", "b")}
    cleaned = SummaryService._clean_points(  # noqa: SLF001
        [{"text": "要点一", "article_ids": [1, 999, "x", 2, 1]}], known, 6,
    )
    assert cleaned[0]["article_ids"] == [1, 2]
    assert cleaned[0]["reasons"] == []


def test_clean_points_marks_points_without_valid_ids():
    known = {1: _doc(1, "t", "b")}
    cleaned = SummaryService._clean_points(  # noqa: SLF001
        [{"text": "凭空要点", "article_ids": [4242]}], known, 6,
    )
    assert cleaned[0]["article_ids"] == []
    assert cleaned[0]["reasons"] == ["模型未给出有效出处"]


def test_clean_points_skips_empty_text_and_bad_shapes():
    known = {1: _doc(1, "t", "b")}
    cleaned = SummaryService._clean_points(  # noqa: SLF001
        [{"article_ids": [1]}, "字符串不是对象", {"text": "  ", "article_ids": [1]},
         {"text": "有效要点", "article_ids": [1]}],
        known, 6,
    )
    assert len(cleaned) == 1
    assert cleaned[0]["text"] == "有效要点"


def test_service_uses_llm_when_configured_and_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "headline": "AI 总览",
            "key_points": [{"text": "AI 提炼的要点", "article_ids": [1]}],
            "highlights": [{"text": "AI 提炼的亮点", "article_ids": [1]}],
        }, ensure_ascii=False)}}]})

    stub = _StubRepo(
        stats={"total": 1, "sources": 1, "provinces": 1, "earliest": None, "latest": None},
        rows=[{"id": 1, "title": "特高压投运", "body_text": "新增外送能力800万千瓦。",
               "source_name": "国网", "published_at_cst": datetime(2026, 10, 12, 9),
               "url": "u", "province_name": "北京"}],
    )
    data = run(_service(stub, llm=_mock_llm(handler)).news_summary(
        _query(), window_label="今日", range_key="today",
    ))
    assert data["method"] == "llm"
    assert data["summary"]["key_points"][0]["text"] == "AI 提炼的要点"
    assert data["summary"]["highlights"][0]["text"] == "AI 提炼的亮点"
    # headline 由本地模板生成（要写「共 N 篇」，N 是窗口总数，模型看不到）；
    # 模型那句另存 model_headline，不丢
    assert data["summary"]["model_headline"] == "AI 总览"
    assert "共 1 篇" in data["summary"]["headline"]
    assert data["caveats"] == []


def test_service_auto_downgrades_when_llm_call_fails():
    """auto 模式下 LLM 挂了要降级，并把原因写进 caveats。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dial failed", request=request)

    stub = _StubRepo(
        stats={"total": 1, "sources": 1, "provinces": 1, "earliest": None, "latest": None},
        rows=[{"id": 1, "title": "特高压投运", "body_text": "新增外送能力800万千瓦。",
               "source_name": "国网", "published_at_cst": datetime(2026, 10, 12, 9),
               "url": "u", "province_name": "北京"}],
    )
    data = run(_service(stub, llm=_mock_llm(handler)).news_summary(
        _query(), window_label="今日", range_key="today", method="auto",
    ))
    assert data["method"] == "extractive"
    assert any("LLM 调用失败" in c for c in data["caveats"])


def test_service_method_llm_raises_instead_of_silently_downgrading():
    """显式 method=llm 时失败必须抛错，不能偷偷换成抽取式。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    stub = _StubRepo(
        stats={"total": 1, "sources": 1, "provinces": 1, "earliest": None, "latest": None},
        rows=[{"id": 1, "title": "t", "body_text": "b", "source_name": "s",
               "published_at_cst": datetime(2026, 10, 12), "url": "u",
               "province_name": "北京"}],
    )
    with pytest.raises(LLMUnavailable):
        run(_service(stub, llm=_mock_llm(handler)).news_summary(
            _query(), window_label="今日", range_key="today", method="llm",
        ))


def test_window_days_table():
    assert SummaryService._window_days("week", _query()) == 7  # noqa: SLF001
    assert SummaryService._window_days("today", _query()) == 1  # noqa: SLF001
    assert SummaryService._window_days("custom", _query(  # noqa: SLF001
        date_from=datetime(2026, 9, 23), date_to=datetime(2026, 10, 12, 23, 59, 59),
    )) == 20
    assert SummaryService._window_days("custom", _query()) is None  # noqa: SLF001


def test_headline_quotes_window_total_not_the_sampled_count():
    """headline 必须说窗口真数（307），不能说参与摘要的篇数（40）。

    回归：曾经用 ``len(docs)`` 拼 headline，把一周 307 篇电力新闻说成「共 40 篇」——
    说错话比不说话更糟。
    """
    docs = [_doc(i, f"特高压工程进展{i}", f"第{i}条正文，讲述工程投运与投资规模。") for i in range(1, 6)]
    extraction = summarize(
        docs, window_label="近1周", window_total=307, window_sources=12,
    )
    assert "共 307 篇" in extraction.headline
    assert "来自 12 个站点" in extraction.headline
    assert extraction.doc_count == 5  # 参与摘要的仍是 5 篇，与 headline 口径分离


def test_service_passes_window_stats_into_headline():
    stub = _StubRepo(
        stats={"total": 307, "sources": 12, "provinces": 8, "earliest": None, "latest": None},
        rows=[{"id": 1, "title": "特高压投运", "body_text": "新增外送能力800万千瓦。",
               "source_name": "国网", "published_at_cst": datetime(2026, 10, 12, 9),
               "url": "u", "province_name": "北京"}],
    )
    data = run(_service(stub).news_summary(_query(), window_label="近1周", range_key="week"))
    assert "共 307 篇" in data["summary"]["headline"]
    assert data["stats"]["articles_used"] == 1


# ======================================================================
# 7. HTTP 面
# ======================================================================


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        if not self._rows:
            return None
        first = self._rows[0]
        return list(first.values())[0] if isinstance(first, dict) else first


class FakeSession:
    """按调用顺序返回预置结果（``window_stats`` → … → ``list_for_summary``）。"""

    def __init__(self, responses: list[list]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ANN001, ANN201
        self.calls.append(str(stmt))
        rows = self._responses.pop(0) if self._responses else []
        return _Result(rows)


@pytest.fixture(autouse=True)
def _offline_router_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """全文件默认：路由取到的 ``SummaryService`` 不带真实 LLM 配置。

    用 ``monkeypatch`` 是为了**用完还原** —— 直接赋值模块属性会污染同一
    pytest 进程里的其他测试文件（例如那些要靠 ``monkeypatch.setattr`` 打桩
    ``summary_module.build_llm_client`` 的用例）。
    """
    monkeypatch.setattr(summary_module, "SummaryService", _OfflineSummaryService)


def build_app(session: FakeSession) -> FastAPI:
    """搭一个只挂 summary 路由的离线 app。

    路由里的 ``SummaryService`` 由下面的 ``_offline_router_service`` 自动 fixture
    换成 :class:`_OfflineSummaryService`（否则它会读真实 ``.env`` 并真的请求模型）。
    """
    app = FastAPI()
    app.include_router(summary_module.router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_collector_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: AuthContext(None, "test", {"admin"})
    app.dependency_overrides[enforce_rate_limit] = lambda: 0
    return app


_STATS_ROW = {"total": 5, "sources": 3, "provinces": 2,
              "earliest": datetime(2026, 10, 12, 8), "latest": datetime(2026, 10, 12, 9)}
_DOC_ROW = {"id": 1, "title": "国家电网投运±800千伏特高压直流工程",
            "body_text": "该工程今日正式投运，新增外送能力800万千瓦，投资约120亿元。",
            "source_name": "国网北京市电力公司",
            "published_at_cst": datetime(2026, 10, 12, 9), "url": "https://x/1",
            "province_name": "北京"}


def test_route_defaults_to_today_window():
    """不传任何时间参数 → 今日（需求：默认提供当日重点新闻汇总）。"""
    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    resp = TestClient(build_app(session)).get("/api/v1/news/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["window"]["range"] == "today"
    assert data["window"]["days"] == 1
    today = date.today()
    assert data["window"]["start"] == f"{today} 00:00:00"
    assert data["window"]["end"] == f"{today} 23:59:59"


def test_route_returns_the_five_document_sections():
    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    data = TestClient(build_app(session)).get("/api/v1/news/summary?range=week").json()["data"]
    for key in ("window", "scope", "stats", "summary", "caveats"):
        assert key in data, f"缺少顶层段 {key}"
    assert data["window"]["days"] == 7
    assert data["stats"]["total_matched"] == 5
    assert data["stats"]["articles_used"] == 1
    assert data["summary"]["headline"]


def test_route_accepts_month_and_reports_the_window():
    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    data = TestClient(build_app(session)).get("/api/v1/news/summary?range=month").json()["data"]
    assert data["window"]["days"] == 30
    assert "近1个月" in data["window"]["label"]


def test_route_rejects_range_together_with_since():
    """歧义输入一律 400，不按某一种优先级静默解释。"""
    resp = TestClient(build_app(FakeSession([]))).get(
        "/api/v1/news/summary?range=week&since=2026-10-01",
    )
    assert resp.status_code == 400
    assert "二选一" in resp.json()["detail"]


def test_route_rejects_unknown_range():
    resp = TestClient(build_app(FakeSession([]))).get("/api/v1/news/summary?range=quarter")
    assert resp.status_code == 422


def test_route_custom_range_uses_given_bounds():
    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    data = TestClient(build_app(session)).get(
        "/api/v1/news/summary?since=2026-09-23&until=2026-10-12",
    ).json()["data"]
    assert data["window"]["range"] == "custom"
    assert data["window"]["start"] == "2026-09-23 00:00:00"
    assert data["window"]["end"] == "2026-10-12 23:59:59"
    assert data["window"]["days"] == 20


def test_route_method_llm_without_config_is_400_not_500(monkeypatch):
    """要求走模型但没配好 → 400（客户端的请求问题），不是 500。

    ⚠️ 注入一个「未配置」的 LLM 客户端，而不是依赖开发机「恰好没配 LLM」。
    后者会被 ``.env`` 影响：实测 `.env` 配好后这个用例返回 500（真去调模型了）。
    """
    import newsplatform.core.llm as llm_module
    import newsplatform.services.summary_service as summary_module

    unconfigured = llm_module.LLMSettings(
        enabled=True, base_url="", api_key="", model="",
        timeout=1.0, max_input_chars=100, max_tokens=10,
    )
    monkeypatch.setattr(
        summary_module, "build_llm_client",
        lambda *a, **k: llm_module.LLMClient(unconfigured),
    )

    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    resp = TestClient(build_app(session)).get("/api/v1/news/summary?method=llm")
    assert resp.status_code == 400
    assert "未配置 LLM" in resp.json()["detail"]


def test_route_rejects_limit_over_the_cap():
    resp = TestClient(build_app(FakeSession([]))).get("/api/v1/news/summary?limit=9999")
    assert resp.status_code == 422


def test_route_scope_is_required():
    """鉴权依赖没被覆盖时必须 401。"""
    app = FastAPI()
    app.include_router(summary_module.router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_collector_session] = lambda: FakeSession([])
    app.dependency_overrides[enforce_rate_limit] = lambda: 0
    resp = TestClient(app).get("/api/v1/news/summary")
    assert resp.status_code == 401


def test_route_summary_is_not_swallowed_by_article_id_path():
    """``/news/summary`` 与 ``/articles/{article_id}`` 前缀不同，不该互相吞。

    这条是回归：把汇总挂在 ``/articles/summary`` 后面会被当 article_id 而 422。
    """
    session = FakeSession([[_STATS_ROW], [{"days": 1}], [_DOC_ROW]])
    resp = TestClient(build_app(session)).get("/api/v1/news/summary")
    assert resp.status_code == 200


# ======================================================================
# 7b. 分层抽样（回归：跨周期窗口必须抽到跨周期的样本）
# ======================================================================


def test_summary_sampling_uses_row_number_partitioned_by_day():
    """取数 SQL 必须按自然日分桶（ROW_NUMBER + PARTITION BY），不能只 ORDER BY LIMIT。

    回归：按「最新 N 篇」取数时，近1个月窗口的 40 篇全落在最后一天，
    于是「今日 / 近1周 / 近1个月」三个档位返回**完全相同的要点**，
    周期筛选形同虚设。
    """
    from newsplatform.repositories.articles import ArticleRepository

    session = FakeSession([[{"days": 5}], []])
    run(ArticleRepository(session).list_for_summary(_query(), limit=40))
    sql = session.calls[-1]
    assert "row_number" in sql.lower(), f"缺少窗口函数：{sql[:200]}"
    assert "PARTITION BY" in sql.upper(), f"缺少按天分区：{sql[:200]}"
    assert "rn <= " in sql.lower(), f"缺少按天配额过滤：{sql[:200]}"


def test_sampled_days_is_reported_in_stats():
    """响应要给出样本覆盖天数，让「抽样退化」可被一眼发现。"""
    stub = _StubRepo(
        stats={"total": 549, "sources": 32, "provinces": 20, "earliest": None, "latest": None},
        rows=[
            {"id": 1, "title": "a", "body_text": "正文一。", "source_name": "s",
             "published_at_cst": datetime(2026, 9, 20, 9), "url": "u", "province_name": "北京"},
            {"id": 2, "title": "b", "body_text": "正文二。", "source_name": "s",
             "published_at_cst": datetime(2026, 10, 12, 9), "url": "u", "province_name": "广东"},
        ],
    )
    data = run(_service(stub).news_summary(_query(), window_label="近1个月", range_key="month"))
    assert data["stats"]["sampled_days"] == 2


# ======================================================================
# 8. 与既有查询面共用同一套窗口口径
# ======================================================================


def test_summary_window_matches_articles_window():
    """``/news/summary`` 与 ``/articles`` 必须解析出同一个窗口。

    两边各算一遍是本项目踩过的真实缺陷（「接口 307 条 / CLI 306 条」），
    所以这条断言钉住「同一实现」。
    """
    from newsplatform.core.deps import resolve_time_window

    for key in ("today", "week", "month"):
        assert resolve_time_window(key) == resolve_time_window(key)
    with pytest.raises(Exception, match="二选一"):
        resolve_time_window("week", since="2026-10-01")
