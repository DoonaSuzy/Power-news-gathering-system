"""下载中间件：合规决策与站点限速。

``AccessPolicyMiddleware``（优先级 560）
    替代 Scrapy 内置 RobotsTxtMiddleware，支持**按来源**声明策略：

    - ``obey``：尊重 robots.txt（默认）；
    - ``ignore``：明确豁免（例如公开数据、已获授权、教育研究用途，
      且该豁免必须写进来源清单文档与 README 的「已知注意点」）；
    - ``custom``：交由适配器的 ``robots_allowed()`` 自行判断。

    内置中间件已被关掉（见 ``settings.py``），这里是唯一的合规决策点。

``PaceMiddleware``（优先级 590）
    按来源声明的 ``request_interval`` 设置下载槽延迟，实现「敏感站 5 秒/并发 1」
    这类差异化限速；对未声明的来源不做任何额外限速（交给 AutoThrottle）。
"""

from __future__ import annotations

from newsagg.logutil import get_logger

log = get_logger("middleware")

__all__ = ["AccessPolicyMiddleware", "PaceMiddleware", "ROBOTS_OBEY", "ROBOTS_IGNORE"]

ROBOTS_OBEY = "obey"
ROBOTS_IGNORE = "ignore"
ROBOTS_CUSTOM = "custom"

#: robots.txt 缓存时长（秒）
_ROBOTS_TTL = 3600
#: robots.txt 拉取超时（秒）
_ROBOTS_TIMEOUT = 8


class AccessPolicyMiddleware:
    """按来源策略决定是否放行请求。"""

    def __init__(self, *, default_policy: str = ROBOTS_OBEY,
                 overrides: dict[str, str] | None = None) -> None:
        self.default_policy = default_policy
        self.overrides = dict(overrides or {})
        self._robots: dict[str, object] = {}

    @classmethod
    def from_crawler(cls, crawler):  # noqa: ANN001
        settings = crawler.settings
        return cls(
            default_policy=settings.get("POWERNEWS_ROBOTS_POLICY", ROBOTS_OBEY),
            overrides=settings.getdict("POWERNEWS_ROBOTS_OVERRIDES") or {},
        )

    # ------------------------------------------------------------------
    def scope(self) -> str:
        """本中间件作用的来源名（由 spider 在请求 meta 中带出）。"""
        return "powernews.crawl.middlewares.AccessPolicyMiddleware"

    def _policy_for(self, source: str) -> str:
        if not source:
            return self.default_policy
        return self.overrides.get(source, self.default_policy)

    def _robots_parser(self, url: str):
        """取（带缓存的）robots 解析器；不可用时返回 None（放行）。"""
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is not None:
            return cached or None

        parser = RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            from newsagg.crawl.dedup import DEFAULT_HASHES  # noqa: F401  仅确保包内可用

            import urllib.request

            req = urllib.request.Request(
                robots_url,
                headers={
                    "User-Agent": "powernews/0.1 (educational local aggregator)",
                    "Accept": "text/plain,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=_ROBOTS_TIMEOUT) as resp:
                parser.parse(resp.read().decode("utf-8", errors="replace").splitlines())
        except Exception as exc:  # noqa: BLE001
            # robots.txt 不可达时按「放行」处理，但记录一次，便于排查
            log.debug("robots.txt 不可达 %s: %s", robots_url, exc)
            self._robots[origin] = False
            return None

        self._robots[origin] = parser
        return parser

    def process_request(self, request, spider):  # noqa: ANN001
        """返回 Response 表示直接短路；返回 None 表示放行。"""
        source = request.meta.get("powernews_source") or getattr(spider, "source_id", "")
        policy = self._policy_for(source)

        if policy == ROBOTS_IGNORE:
            request.meta.setdefault("powernews_robots_decision", "ignore")
            return None

        if policy == ROBOTS_CUSTOM:
            adapter = getattr(spider, "adapter", None)
            allowed = True
            if adapter is not None and hasattr(adapter, "robots_allowed"):
                try:
                    allowed = bool(adapter.robots_allowed(request.url))
                except Exception:  # noqa: BLE001
                    allowed = True
            request.meta.setdefault(
                "powernews_robots_decision", "custom_allow" if allowed else "custom_deny"
            )
            return None if allowed else self._deny(request)

        parser = self._robots_parser(request.url)
        if parser is None:
            request.meta.setdefault("powernews_robots_decision", "robots_unavailable")
            return None

        user_agent = request.headers.get("User-Agent", b"powernews").decode(
            "utf-8", errors="replace"
        )
        if parser.can_fetch(user_agent, request.url):
            request.meta.setdefault("powernews_robots_decision", "allow")
            return None
        return self._deny(request)

    @staticmethod
    def _deny(request):  # noqa: ANN001
        """按 robots 拒绝：返回一个 403 响应，交由蜘蛛的失败分支处理。"""
        import scrapy

        log.info("robots.txt 拒绝，跳过: %s", request.url)
        return scrapy.http.Response(
            url=request.url, status=403, request=request,
            headers={"X-PowerNews-Robots": "denied"},
        )


class PaceMiddleware:
    """按来源声明的间隔设置下载槽延迟。"""

    def __init__(self, *, default_delay: float = 0.0) -> None:
        self.default_delay = default_delay
        self._applied: dict[str, float] = {}

    @classmethod
    def from_crawler(cls, crawler):  # noqa: ANN001
        return cls(default_delay=float(crawler.settings.get("POWERNEWS_DEFAULT_DELAY", 0) or 0))

    def process_request(self, request, spider):  # noqa: ANN001
        slot = getattr(request, "meta", {}).get("powernews_slot")
        interval = request.meta.get("powernews_interval")
        if not slot or not interval:
            return None

        try:
            delay = float(interval)
        except (TypeError, ValueError):
            return None

        if self._applied.get(slot) == delay:
            return None

        # 首次见到该槽位时设置其延迟；Scrapy 的 DownloadSlots 保证同槽串行
        try:
            from scrapy.core.downloader import slots as _slots  # noqa: F401
        except Exception:  # noqa: BLE001
            pass

        downloader = getattr(spider.crawler.engine, "downloader", None) if hasattr(spider, "crawler") else None
        slot_obj = getattr(downloader, "slots", {}).get(slot) if downloader else None
        if slot_obj is not None:
            slot_obj.delay = delay
            self._applied[slot] = delay
            log.info("下载槽 %s 延迟设为 %.1f 秒", slot, delay)
        return None
