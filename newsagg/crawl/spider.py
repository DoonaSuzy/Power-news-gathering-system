"""站点蜘蛛 ``SiteSpider``。

⚠️ Scrapy 2.19 契约：``start()`` 必须是**异步生成器**（框架校验 ``__aiter__``）。
同时保留 ``start_requests()`` 以兼容旧式入口。

⚠️ 平台约定：不安装 ``AsyncioSelectorReactor``，见 ``crawl/__init__.py``。

入口支持**多栏目页**：适配器的 ``extra_column_urls`` 会在主栏目页之外一并
作为起始页，解决「单个栏目页只有十几条候选」的覆盖问题。
"""

from __future__ import annotations

import re
from datetime import timedelta

import scrapy

from newsagg.adapters import ensure_loaded, get_adapter
from newsagg.adapters.base import DetailParse
from newsagg.cleaning import (
    clean_paragraphs,
    clean_source,
    clean_title,
    content_hash,
    parse_published,
    quality_flags,
    to_utc_naive,
)
from newsagg.crawl.mimetypes import is_non_text_page
from newsagg.crawl.sampler import Sampler
from newsagg.crawl.session import CrawlSession
from newsagg.logutil import bind_context, get_logger
from newsagg.rawstore import raw_store_for
from newsagg.urls import normalize_url, url_hash

log = get_logger("spider")

__all__ = ["SiteSpider", "js_redirect_target"]

#: 纯 JS 跳转壳页：``<script>location.href = "..."</script>``。
#: 典型如部分新闻网把老路径重定向到真实的稿件页，静态正文为空，
#: 不跟随就只能存一条空壳记录。
_JS_REDIRECT = re.compile(
    r"""(?:location\.href|window\.location|location\.replace)\s*=\s*["']([^"']+)["']""",
    re.I,
)
#: 壳页体积上限：正文页不可能只有这么点内容
_SHELL_MAX_BYTES = 4096


def js_redirect_target(text: str) -> str:
    """壳页里唯一的 JS 跳转目标；非壳页返回空串。"""
    if not text or len(text) > _SHELL_MAX_BYTES:
        return ""
    match = _JS_REDIRECT.search(text)
    return match.group(1).strip() if match else ""


class SiteSpider(scrapy.Spider):
    """单来源抓取蜘蛛。``session`` 与 ``list_url`` 由 engine 注入。"""

    name = "site_spider"

    def __init__(self, session: CrawlSession, list_url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self.list_url = list_url
        self.source_id = session.adapter_name
        ensure_loaded()
        self.adapter = get_adapter(session.adapter_name)
        self.sampler = Sampler(session.settings.samples_dir)
        # 原始响应落盘层（默认关闭，用 POWERNEWS_KEEP_RAW=true 打开）。
        # 它解决的是「改解析规则必须重抓」—— 存了原文就能重洗。
        self.raw = raw_store_for(session.adapter_name)
        self._visited_pages: set[str] = set()

    # ------------------------------ 入口 ------------------------------
    def _entry_urls(self) -> list[str]:
        """本次任务的入口栏目页（主栏目 + 适配器声明的扩展频道）。"""
        urls = [self.list_url]
        for extra in getattr(self.adapter, "extra_column_urls", ()) or ():
            if extra and extra not in urls:
                urls.append(extra)
        return urls

    def _request(self, url: str, callback, **kwargs):  # noqa: ANN001
        """构造请求，统一带上来源与限速 meta。"""
        meta = dict(kwargs.pop("meta", {}) or {})
        meta["powernews_source"] = self.session.adapter_name
        interval = getattr(self.adapter, "request_interval", 0.0) or 0.0
        if interval:
            meta["powernews_slot"] = self.session.adapter_name
            meta["powernews_interval"] = interval
        return scrapy.Request(
            url, callback=callback, dont_filter=True, meta=meta,
            errback=kwargs.pop("errback", self.on_error), **kwargs
        )

    async def start(self):
        """Scrapy 2.13+ 的真实入口（必须是异步生成器）。"""
        for url in self._entry_urls():
            yield self._request(url, self.parse_list)

    def start_requests(self):
        """兼容旧式入口。"""
        for url in self._entry_urls():
            yield self._request(url, self.parse_list)

    # ------------------------------ 列表页 ------------------------------
    def parse_list(self, response):  # noqa: ANN001
        if self._guard():
            return
        self.session.requests += 1

        if response.status != 200:
            self.sampler.save_failure(response.url, response.status, response.text,
                                      label="list")
            if response.status in (403, 405, 412, 429):
                self.session.note_block(response.status)
            self.session.request_stop("list_fetch_failed")
            return

        parsed = self.adapter.parse_list(response.text, base_url=response.url)
        if not parsed.descending:
            self.session.request_stop("list_not_descending")
            return

        oldest = self._oldest_list_time(parsed)
        # ⚠️ 这里只能**判断**「是否已翻到窗口起点」，**不能**直接 request_stop()。
        #
        # ``should_stop`` 是全局的，而下面遍历候选项时每轮都有 ``_guard()`` 拦截；
        # 一旦在循环前停机，循环第一项就会 return，**本页的候选一条都不会被处理**。
        # 现象是「列表页里只要有一条窗口外的旧文章 → requests=1 / discovered=0」，
        # 新源首采直接空转（实测 chinapower、china5e 都是这样，且难以从日志看出原因）。
        #
        # 正确顺序：先处理完本页候选，再决定是否停止**翻页**。
        reached_window_start = (
            oldest is not None and oldest < self.session.window.start_cst
        )
        if not reached_window_start:
            self.session.mark_discovery_success()

        # 列表页解析出的条目数（**无论新旧**）。
        # 判「列表页是否真空」看这个，不是 discovered —— 增量采集时
        # 站点没更新会让 discovered=0 但 listed 正常，两者混用会误判异常。
        self.session.listed += len(parsed.items)

        for item in parsed.items:
            if self._guard():
                return
            nh = url_hash(item.url)
            if not self.session.consider(item.url, nh, list_title=item.title):
                continue
            yield self._request(
                item.url, self.parse_detail,
                meta={"powernews_url_hash": nh},
            )

        if reached_window_start:
            # 本页已处理完，翻到窗口起点即停止**继续翻页**。
            #
            # ⚠️ 必须用 ``stop_paging_only`` 而不是 ``request_stop``：
            # 上面 26 个候选的详情请求已经 yield 出去、正在 Scrapy 队列里排队，
            # 若设了全局 ``should_stop``，``parse_detail`` 开头的 ``_guard()``
            # 会把它们**全部丢弃**（实测 discovered=26 / requests=1 / saved=0）。
            self.session.stop_paging_only("window_start_reached")
            return

        # 翻页
        if parsed.next_url and not self.session.stop_paging and not self._guard():
            nxt = normalize_url(parsed.next_url)
            if nxt not in self._visited_pages:
                self._visited_pages.add(nxt)
                yield self._request(parsed.next_url, self.parse_list)

    @staticmethod
    def _oldest_list_time(parsed):
        """列表页中最旧的一条时间（用于窗口下界提前停止）。"""
        from datetime import datetime

        date_re = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
        dates = []
        for item in parsed.items:
            match = date_re.search(item.list_time_text or "")
            if not match:
                continue
            try:
                dates.append(
                    datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                )
            except ValueError:
                continue
        return min(dates) if dates else None

    # ------------------------------ 详情页 ------------------------------
    def parse_detail(self, response):  # noqa: ANN001
        if self._guard():
            return
        self.session.requests += 1
        nh = response.meta.get("powernews_url_hash") or url_hash(response.url)

        # 原始响应落盘：**在解析之前**，且**不论解析成败**都存。
        #
        # 位置很关键 —— 放在解析之后的话，「解析失败」的页面就不会留档，
        # 而那恰恰是最需要原文的场景（改选择器时要看当时的 DOM）。
        # 存的是未解码的 bytes：原始字节才能保证编码可追溯。
        self.raw.save(response.url, nh, response.body, status=response.status)

        if response.status in (404, 410):
            self.session.mark_task(nh, "skipped", skip_reason=f"http_{response.status}")
            return
        if response.status != 200:
            if response.status in (403, 405, 412, 429):
                self.session.note_block(response.status)
            self.session.mark_task(nh, "failed", failure_class=f"http_{response.status}")
            return

        if is_non_text_page(response.url, response.text):
            self.session.mark_task(nh, "skipped", skip_reason="non_text")
            return

        # 纯 JS 跳转壳页：跟随真实稿件地址再解析一次（只跳一层）
        target = js_redirect_target(response.text)
        if target and not response.meta.get("powernews_js_redirected"):
            yield self._request(
                target, self.parse_detail,
                meta={"powernews_url_hash": nh, "powernews_js_redirected": True},
            )
            return

        detail = self.adapter.parse_detail(response.text, url=response.url)
        parsed = parse_published(
            detail.published_text, default_year=self.session.window.end_cst.year
        )
        if parsed.value is not None and parsed.value > self.session.window.end_cst:
            self.session.mark_task(nh, "skipped", skip_reason="above_window")
            return

        self._store(detail, response.url, nh, parsed)
        self.session.mark_task(nh, "done")

    def _store(self, detail: DetailParse, url: str, nh: str, parsed) -> None:
        paragraphs = clean_paragraphs(detail.paragraphs)
        flags = quality_flags(paragraphs, is_media_only=detail.media_only)
        cst = parsed.value

        # 时间可信度护栏：页脚/版权行里的年份会被误当成发布时间
        if cst is not None and (
            cst.year < 2000 or cst > self.session.window.end_cst + timedelta(days=1)
        ):
            cst = None

        utc = to_utc_naive(cst) if cst else None
        # 详情页标题缺失时，兜底用列表页标题
        title = clean_title(detail.title) or clean_title(self.session.list_title_for(nh))

        self.session.store_detail(
            url=normalize_url(url),
            url_hash=nh,
            title=title,
            paragraphs=paragraphs,
            source_name=clean_source(detail.source_name),
            published_cst=cst,
            published_utc=utc,
            time_precision=(parsed.precision if cst else "unknown"),
            content_hash=content_hash(paragraphs),
            quality_flags=flags,
        )

    # ------------------------------ 守卫与错误 ------------------------------
    def _guard(self) -> bool:
        """租约丢失 / 被封 / 超预算时返回 True。"""
        if self.session.should_stop:
            return True
        return self.session.budget_hit()

    def on_error(self, failure):  # noqa: ANN001
        """请求失败：分类并写样本，不致命则跳过该条。"""
        from scrapy.spidermiddlewares.httperror import HttpError

        url = failure.request.url
        nh = failure.request.meta.get("powernews_url_hash") or url_hash(url)
        code = "unknown"
        if failure.check(HttpError) and getattr(failure.value, "response", None) is not None:
            status = failure.value.response.status
            code = f"http_{status}"
            if status in (403, 405, 412, 429):
                self.session.note_block(status)
        elif failure.check(Exception):
            code = type(failure.value).__name__.lower()
        log.info("详情失败 %s: %s", url, code)
        self.session.mark_task(nh, "failed", failure_class=code)
        return None

    def closed(self, reason: str) -> None:
        """蜘蛛关闭：记录停机原因。"""
        bind_context(job_id=self.session.job_id, adapter=self.session.adapter_name)
        log.info(
            "spider 关闭 job=%s reason=%s stop=%s discovered=%s saved=%s",
            self.session.job_id, reason, self.session.stop_reason,
            self.session.discovered, self.session.saved,
        )
        if not self.session.stop_reason:
            self.session.stop_reason = reason
