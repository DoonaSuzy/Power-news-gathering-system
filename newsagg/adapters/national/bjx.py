"""全国性行业来源 —— 北极星电力网（bjx）。

来源清单：https://www.www.bjx.com.cn
建议采集栏目：省级标签页

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class BjxAdapter(SgccGenericAdapter):
    """北极星电力网。"""

    source_id = "bjx"
    source_name = "北极星电力网"
    province_code = None
    category = "industry"
    priority = 2
    crawl_interval_hours = 4
    list_url = "https://www.bjx.com.cn/"
    column_url = "https://www.bjx.com.cn/"
    allowed_hosts = ("www.bjx.com.cn",)
    language = "zh-CN"


register(BjxAdapter())
