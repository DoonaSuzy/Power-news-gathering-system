"""四川 —— 国网四川省电力公司（sgcc_sc）。

来源清单：https://www.www.sc.sgcc.com.cn
建议采集栏目：公司要闻 / 媒体聚焦

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class SgccScAdapter(SgccGenericAdapter):
    """国网四川省电力公司。"""

    source_id = "sgcc_sc"
    source_name = "国网四川省电力公司"
    province_code = '510000'
    category = "sgcc"
    priority = 0
    crawl_interval_hours = 1
    list_url = "https://www.sc.sgcc.com.cn/html/main/col81/"
    column_url = "https://www.sc.sgcc.com.cn/html/main/col81/"
    allowed_hosts = ("www.sc.sgcc.com.cn",)
    language = "zh-CN"


register(SgccScAdapter())
