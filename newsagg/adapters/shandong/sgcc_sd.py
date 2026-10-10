"""山东 —— 国网山东省电力公司（sgcc_sd）。

来源清单：https://www.www.sd.sgcc.com.cn
建议采集栏目：公司要闻 / 一线风采

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class SgccSdAdapter(SgccGenericAdapter):
    """国网山东省电力公司。"""

    source_id = "sgcc_sd"
    source_name = "国网山东省电力公司"
    province_code = '370000'
    category = "sgcc"
    priority = 0
    crawl_interval_hours = 1
    list_url = "https://www.sd.sgcc.com.cn/p1/"
    column_url = "https://www.sd.sgcc.com.cn/p1/"
    allowed_hosts = ("www.sd.sgcc.com.cn",)
    language = "zh-CN"


register(SgccSdAdapter())
