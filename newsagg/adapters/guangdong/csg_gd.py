"""广东 —— 广东电网有限责任公司（csg_gd）。

**采集策略**：广东电网官网 ``www.gd.csg.cn`` 为 SPA + 动态防护，
静态抓取只能拿到 JS 空壳。故改从南方电网集团总部站
``https://www.csg.cn/xwzx/qygg/`` 采集，并按关键词过滤出广东相关新闻。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.csg_generic import CsgGenericAdapter


class CsgGdAdapter(CsgGenericAdapter):
    """广东电网有限责任公司。"""

    source_id = "csg_gd"
    source_name = "广东电网有限责任公司"
    province_code = "440000"
    priority = 0
    crawl_interval_hours = 1
    language = "zh-CN"

    #: 归属本省的关键词（用于从集团新闻中筛出广东相关条目）
    province_keywords = ("广东电网", "广州供电局", "深圳供电局", "广东")


register(CsgGdAdapter())
