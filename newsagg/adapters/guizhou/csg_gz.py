"""贵州 —— 贵州电网有限责任公司（csg_gz）。

**采集策略**：贵州 电网官网 www.gz.csg.cn 为 SPA + 动态防护，
静态抓取只能拿到 JS 空壳。故改从南方电网集团总部站
``https://www.csg.cn/xwzx/qygg/`` 采集，并按关键词过滤出贵州相关新闻。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.csg_generic import CsgGenericAdapter


class CsgGzAdapter(CsgGenericAdapter):
    """贵州电网有限责任公司。"""

    source_id = "csg_gz"
    source_name = "贵州电网有限责任公司"
    province_code = "520000"
    priority = 0
    crawl_interval_hours = 1
    language = "zh-CN"

    province_keywords = ("贵州电网", "贵州")


register(CsgGzAdapter())
