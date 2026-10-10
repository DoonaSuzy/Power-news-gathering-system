"""海南 —— 海南电网有限责任公司（csg_hn）。

**采集策略**：海南 电网官网 www.hn.csg.cn 为 SPA + 动态防护，
静态抓取只能拿到 JS 空壳。故改从南方电网集团总部站
``https://www.csg.cn/xwzx/qygg/`` 采集，并按关键词过滤出海南相关新闻。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.csg_generic import CsgGenericAdapter


class CsgHnAdapter(CsgGenericAdapter):
    """海南电网有限责任公司。"""

    source_id = "csg_hn"
    source_name = "海南电网有限责任公司"
    province_code = "460000"
    priority = 0
    crawl_interval_hours = 1
    language = "zh-CN"

    province_keywords = ("海南电网", "海南")


register(CsgHnAdapter())
