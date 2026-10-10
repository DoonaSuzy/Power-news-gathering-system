"""云南 —— 云南电网有限责任公司（csg_yn）。

**采集策略**：云南电网官网 www.yn.csg.cn 为 SPA + 动态防护，
静态抓取只能拿到 JS 空壳。故改从南方电网集团总部站
``https://www.csg.cn/xwzx/qygg/`` 采集，并按关键词过滤出云南相关新闻。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.csg_generic import CsgGenericAdapter


class CsgYnAdapter(CsgGenericAdapter):
    """云南电网有限责任公司。"""

    source_id = "csg_yn"
    source_name = "云南电网有限责任公司"
    province_code = "530000"
    priority = 0
    crawl_interval_hours = 1
    language = "zh-CN"

    province_keywords = ("云南电网", "云南")


register(CsgYnAdapter())
