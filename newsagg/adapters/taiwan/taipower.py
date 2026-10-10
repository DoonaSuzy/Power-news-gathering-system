"""中国台湾 —— 台湾电力股份有限公司（taipower）。

来源清单：https://www.www.taipower.com.tw
建议采集栏目：电力脉动 / 公司消息

本文件由脚手架按 ``sources_seed.SOURCES`` 生成。默认继承电网站群通用适配器，
站点改版或正文由 JS 注入时，在此覆写 ``parse_list`` / ``parse_detail``。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.sgcc_generic import SgccGenericAdapter


class TaipowerAdapter(SgccGenericAdapter):
    """台湾电力股份有限公司。"""

    source_id = "taipower"
    source_name = "台湾电力股份有限公司"
    province_code = '710000'
    category = "hmt"
    priority = 1
    crawl_interval_hours = 4
    list_url = "https://www.taipower.com.tw/"
    column_url = "https://www.taipower.com.tw/"
    allowed_hosts = ("www.taipower.com.tw",)
    language = "zh-TW"


register(TaipowerAdapter())
