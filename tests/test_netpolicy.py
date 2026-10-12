"""网络访问策略（协议降级 / 代理清理 / 阻断识别）的回归测试。

对应 `docs/省份攻克日志/README.md` §1.3 的「国网三件套」，三个都钉住：
清代理变量、协议降级、阻断码分类。这些是 27 个国网来源能否采到的前提。
"""

from __future__ import annotations

import pytest

from newsagg.crawl.netpolicy import (
    BLOCK_KINDS,
    PROXY_ENV_KEYS,
    classify_block,
    is_protocol_downgrade,
    protocol_fallbacks,
    strip_proxy_env,
)


class TestStripProxyEnv:
    def test_removes_common_proxy_keys(self):
        env = {
            "http_proxy": "http://127.0.0.1:35004",
            "https_proxy": "http://127.0.0.1:35004",
            "HTTP_PROXY": "http://x",
            "HOME": "/root",
        }
        removed = strip_proxy_env(env)
        assert set(removed) == {"http_proxy", "https_proxy", "HTTP_PROXY"}
        assert "http_proxy" not in env and "HTTP_PROXY" not in env
        assert env["HOME"] == "/root", "无关变量不该被动"

    def test_keeps_no_proxy(self):
        """★ no_proxy 必须保留 —— 内网直连依赖它，清了会把问题扩大。"""
        env = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "192.168.0.0/16"}
        removed = strip_proxy_env(env)
        assert removed == []
        assert env["no_proxy"] == "127.0.0.1,localhost"
        assert env["NO_PROXY"] == "192.168.0.0/16"

    def test_empty_env_is_safe(self):
        assert strip_proxy_env({}) == []

    def test_removes_all_proxy_keys(self):
        env = dict.fromkeys(PROXY_ENV_KEYS, "http://x")
        removed = strip_proxy_env(env)
        assert set(removed) == set(PROXY_ENV_KEYS)
        assert all(k not in env for k in PROXY_ENV_KEYS)


class TestProtocolFallbacks:
    def test_https_gets_http_fallback(self):
        """★ 核心：国网 HTTPS 被阻断、HTTP(80) 可通。"""
        assert protocol_fallbacks("https://www.tj.sgcc.com.cn/p1/") == [
            "https://www.tj.sgcc.com.cn/p1/",
            "http://www.tj.sgcc.com.cn/p1/",
        ]

    def test_http_is_not_upgraded(self):
        """刻意不做 http→https 升格：站点若只支持 http，试 https 纯属浪费。"""
        assert protocol_fallbacks("http://a.cn/p1/") == ["http://a.cn/p1/"]

    def test_preserves_query_and_fragment(self):
        got = protocol_fallbacks("https://a.cn/list?p=2#top")
        assert got[1] == "http://a.cn/list?p=2#top"

    def test_preserves_port_and_path(self):
        got = protocol_fallbacks("https://a.cn:8443/x/y/")
        assert got[1] == "http://a.cn:8443/x/y/"

    @pytest.mark.parametrize("url", ["ftp://a.cn/x", "file:///tmp/x", "not-a-url"])
    def test_non_http_scheme_untouched(self, url):
        assert protocol_fallbacks(url) == [url]


class TestIsProtocolDowngrade:
    def test_same_host_same_path_is_downgrade(self):
        assert is_protocol_downgrade("https://a.cn/p1/", "http://a.cn/p1/") is True

    def test_different_path_is_not(self):
        assert is_protocol_downgrade("https://a.cn/p1/", "http://a.cn/xwzx/") is False

    def test_different_host_is_not(self):
        assert is_protocol_downgrade("https://a.cn/p1/", "http://b.cn/p1/") is False

    def test_upgrade_is_not_downgrade(self):
        assert is_protocol_downgrade("http://a.cn/p1/", "https://a.cn/p1/") is False


class TestClassifyBlock:
    def test_river_security_412_retry_is_pointless(self):
        """★ 412 是瑞数：必须浏览器执行 JS，原样重试毫无意义。"""
        kind, worth_retry = classify_block(412)
        assert kind == "river_security"
        assert worth_retry is False, "412 不该被当成可重试的普通失败"

    def test_403_maybe_path_level(self):
        """403 多为路径级拒绝 —— 江西实测换路径后入库 40 篇。"""
        kind, worth_retry = classify_block(403)
        assert kind == "path_or_waf"
        assert worth_retry is True

    def test_202_is_risk_queue(self):
        """202 是风控排队（≠ 412），加 Referer 或降频可能就过。"""
        kind, worth_retry = classify_block(202)
        assert kind == "risk_queue"
        assert worth_retry is True

    def test_429_rate_limit_worth_retry(self):
        assert classify_block(429) == ("rate_limit", True)

    @pytest.mark.parametrize("status", [200, 301, 404, 500])
    def test_normal_codes_return_none(self, status):
        assert classify_block(status) is None

    def test_all_block_kinds_have_reason(self):
        for code, (kind, worth) in BLOCK_KINDS.items():
            assert kind and isinstance(worth, bool), f"{code} 定义不完整"


class TestSgccCandidateUrls:
    """国网通用基类的候选栏目路径 —— 江苏 `/p1/`→404 那个坑的修复。"""

    def test_sgcc_offers_xwzx_first(self):
        """候选第一个应是新版站群 `/xwzx/`（江苏靠它从 404 变成 40 篇）。"""
        from newsagg.adapters import ensure_loaded, get_adapter

        ensure_loaded()
        cands = get_adapter("sgcc_js").candidate_list_urls
        assert cands, "国网源必须有候选路径"
        assert "/xwzx/" in cands[0], f"首个候选应为新版入口，实际 {cands[0]}"

    def test_candidates_use_http(self):
        """候选用 http:// —— 国网 HTTPS 多被阻断，省掉必然失败的尝试。"""
        from newsagg.adapters import ensure_loaded, get_adapter

        ensure_loaded()
        for u in get_adapter("sgcc_tj").candidate_list_urls:
            assert u.startswith("http://"), f"候选应以 http 开头: {u}"

    def test_candidates_exclude_list_url(self):
        """候选不含 list_url 本身，否则会自己重试自己。"""
        from newsagg.adapters import ensure_loaded, get_adapter

        ensure_loaded()
        a = get_adapter("sgcc_js")
        assert all(u != a.list_url for u in a.candidate_list_urls)

    def test_all_sgcc_sources_have_candidates(self):
        """27 个国网来源都应拿到候选（基类 property 生效）。"""
        from newsagg.adapters import all_adapters, ensure_loaded

        ensure_loaded()
        sgcc = [a for a in all_adapters().values() if getattr(a, "category", "") == "sgcc"]
        assert len(sgcc) >= 20, f"国网源数量异常: {len(sgcc)}"
        for a in sgcc:
            assert a.candidate_list_urls, f"{a.source_id} 没有候选路径"

