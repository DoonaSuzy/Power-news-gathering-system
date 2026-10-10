"""Scrapy 设置（改造方案 §1.2 采集层配置）。

关键项与理由：

- **并发 8 + AutoThrottle**：目标并发 8，最大延迟 60 秒，被动限速。
- **开启 Cookie**：部分站点用一次性 Cookie（如 ``C3VK`` 类）做反爬校验，
  不带 Cookie 会陷入 302 自跳循环。站点无登录态，无隐私影响。
- **开启重定向（最多 5 跳）**：大量栏目页是 http→https 的 301/302，
  关闭重定向会让列表回调收到 3xx 被 HttpErrorMiddleware 静默丢弃，
  表现为「discovered=0 直接 finished」的空跑。
- **不覆盖 TWISTED_REACTOR**：见 ``crawl/__init__.py`` 的平台约定。
- **DOWNLOAD_SLOTS**：为限流站点（如 5 秒/并发 1）预留独立下载槽。

按来源下发 TLS 套件：Scrapy 仅支持全局 ``DOWNLOADER_CLIENT_TLS_CIPHERS``，
不支持逐请求设置，因此对少数只接受弱套件的老站，由
``engine._adapter_overrides()`` 在**该来源独占的任务进程**内下发，
其余来源保持 OpenSSL 默认套件。
"""

from __future__ import annotations

from newsagg.config import get_settings

__all__ = ["scrapy_settings", "RETRY_HTTP_CODES", "BLOCK_HTTP_CODES"]

#: 需要重试的状态码
RETRY_HTTP_CODES = [500, 502, 503, 504, 522, 524, 408]

#: 视为「访问被阻断」的状态码（触发停机与冷却）
BLOCK_HTTP_CODES = {403, 405, 412, 429}


def scrapy_settings(*, overrides: dict | None = None) -> dict:
    """返回一份 Scrapy 设置字典。"""
    s = get_settings()

    settings: dict = {
        "BOT_NAME": "powernews",
        "CONCURRENT_REQUESTS": s.concurrency,
        "CONCURRENT_REQUESTS_PER_DOMAIN": s.concurrency,
        "DOWNLOAD_TIMEOUT": s.download_timeout,

        # 站点无登录态；开了 Cookie 才能过部分站点的一次性反爬校验
        "COOKIES_ENABLED": True,
        "USER_AGENT": s.user_agent,

        # robots.txt 合规由 AccessPolicyMiddleware 自行决策（支持按来源豁免）
        "ROBOTSTXT_OBEY": False,

        "RETRY_ENABLED": True,
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": RETRY_HTTP_CODES,

        # http→https 的 301/302 必须跟随，否则列表回调收不到响应
        "REDIRECT_ENABLED": True,
        "REDIRECT_MAX_TIMES": 5,

        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": s.concurrency,
        "AUTOTHROTTLE_MAX_DELAY": s.autothrottle_max_delay,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_DEBUG": False,

        "HTTPCACHE_ENABLED": False,
        "LOG_LEVEL": "WARNING",  # Scrapy 自身日志降噪，业务日志走 newsagg.logutil
        "TELNETCONSOLE_ENABLED": False,

        "DOWNLOAD_HANDLERS_BASE": {
            "file": "scrapy.core.downloader.handlers.file.FileDownloadHandler",
            "http": "scrapy.core.downloader.handlers.http.HTTPDownloadHandler",
            "https": "scrapy.core.downloader.handlers.http.HTTPDownloadHandler",
            "data": "scrapy.core.downloader.handlers.datauri.DataURIDownloadHandler",
        },
        "DOWNLOADER_MIDDLEWARES": {
            # 关闭内置 RobotsTxt 中间件，改由 AccessPolicyMiddleware 决策
            "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": None,
            "newsagg.crawl.middlewares.AccessPolicyMiddleware": 560,
            "newsagg.crawl.middlewares.PaceMiddleware": 590,
        },
        # 按来源声明限流槽（慢站/敏感站），键名与 sources.adapter_name 对齐
        "DOWNLOAD_SLOTS": {},
    }

    if overrides:
        settings.update(overrides)
    return settings
