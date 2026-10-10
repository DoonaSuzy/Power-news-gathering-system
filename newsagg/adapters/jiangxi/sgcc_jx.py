"""江西 —— 国网江西省电力有限公司（sgcc_jx）。

来源清单：https://www.www.jx.sgcc.com.cn
建议采集栏目：公司要闻 / 一线风采

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class SgccJxAdapter(SgccGenericAdapter):
    """国网江西省电力有限公司。"""

    source_id = "sgcc_jx"
    source_name = "国网江西省电力有限公司"
    province_code = '360000'
    category = "sgcc"
    priority = 0
    crawl_interval_hours = 1
    list_url = "https://www.jx.sgcc.com.cn/p1/"
    column_url = "https://www.jx.sgcc.com.cn/p1/"
    allowed_hosts = ("www.jx.sgcc.com.cn",)
    language = "zh-CN"


register(SgccJxAdapter())
