"""Scrapy 爬虫子包：蜘蛛、采集会话、下载策略、去重、失败页采样。

⚠️ 平台约定（勿回退）
-------------------
**不要**在本包内安装 ``AsyncioSelectorReactor``。在 Windows + Python 3.13 +
Twisted 26.x 下，强制 asyncio 反应器会让 Scrapy 2.19 的异步蜘蛛
``async start()`` 永久挂起（事件循环未与 Twisted 对接，首个请求永不调度）。
``site_spider.py`` 的 ``start()`` 因此只要求是「异步生成器」，由 Twisted
默认 Windows 反应器驱动。
"""

from __future__ import annotations

__all__ = []
