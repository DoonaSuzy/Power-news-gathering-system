"""中国香港 —— 中华电力有限公司（clp_hk）。

来源清单：https://www.www.clp.com.hk
建议采集栏目：新闻稿 / 公司动态

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class ClpHkAdapter(SgccGenericAdapter):
    """中华电力有限公司。"""

    source_id = "clp_hk"
    source_name = "中华电力有限公司"
    province_code = '810000'
    category = "hmt"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.clp.com.hk/"
    column_url = "https://www.clp.com.hk/"
    allowed_hosts = ("www.clp.com.hk",)
    language = "zh-TW"


register(ClpHkAdapter())
