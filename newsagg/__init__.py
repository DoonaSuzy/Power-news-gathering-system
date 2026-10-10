"""Power News Aggregator —— 电力新闻采集层主包。

覆盖 34 个省级行政区的电力新闻采集工具：定时协调器建批 → Redis Stream
发布任务 → worker 领取 → Scrapy Spider 抓取 → 解析清洗 → MySQL 落库。

⚠️ 平台约定（勿回退）
-------------------
在 Windows + Python 3.13 + Twisted 26.x 环境下，**不要**安装
``AsyncioSelectorReactor``：它会让 Scrapy 2.19 的异步蜘蛛 ``async start()``
永久挂起（事件循环未与 Twisted 对接，首请求永不调度）。
本包不安装任何反应器，交由 Twisted 默认 Windows 反应器驱动。
同时 ``crawl/settings.py`` **不覆盖** ``TWISTED_REACTOR``。
Scrapy 2.19 还要求 ``Spider.start()`` 必须是**异步生成器**。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
