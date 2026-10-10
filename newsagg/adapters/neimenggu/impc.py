"""内蒙古 —— 内蒙古电力（集团）有限责任公司（impc）。

来源清单：https://www.www.impc.com.cn
建议采集栏目：集团动态

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class ImpcAdapter(SgccGenericAdapter):
    """内蒙古电力（集团）有限责任公司。"""

    source_id = "impc"
    source_name = "内蒙古电力（集团）有限责任公司"
    province_code = '150000'
    category = "independent"
    priority = 0
    crawl_interval_hours = 1
    list_url = "https://www.impc.com.cn/"
    column_url = "https://www.impc.com.cn/"
    allowed_hosts = ("www.impc.com.cn",)
    language = "zh-CN"


register(ImpcAdapter())
