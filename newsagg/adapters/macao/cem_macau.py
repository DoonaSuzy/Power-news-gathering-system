"""中国澳门 —— 澳门电力股份有限公司（cem_macau）。

来源清单：https://www.www.cem-macau.com
建议采集栏目：新闻 / 公司动态

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class CemMacauAdapter(SgccGenericAdapter):
    """澳门电力股份有限公司。"""

    source_id = "cem_macau"
    source_name = "澳门电力股份有限公司"
    province_code = '820000'
    category = "hmt"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.cem-macau.com/"
    column_url = "https://www.cem-macau.com/"
    allowed_hosts = ("www.cem-macau.com",)
    language = "zh-TW"


register(CemMacauAdapter())
