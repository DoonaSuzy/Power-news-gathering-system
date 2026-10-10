"""全国性行业来源 —— 中国电力新闻网（cpnn）。

来源清单：https://www.www.cpnn.com.cn
建议采集栏目：地方频道

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class CpnnAdapter(SgccGenericAdapter):
    """中国电力新闻网。"""

    source_id = "cpnn"
    source_name = "中国电力新闻网"
    province_code = None
    category = "industry"
    priority = 1
    crawl_interval_hours = 2
    list_url = "https://www.cpnn.com.cn/"
    column_url = "https://www.cpnn.com.cn/"
    allowed_hosts = ("www.cpnn.com.cn",)
    language = "zh-CN"


register(CpnnAdapter())
