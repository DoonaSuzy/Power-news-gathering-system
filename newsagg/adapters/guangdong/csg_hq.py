"""南方电网集团总部（csg_hq）。

列表页 `/xwzx/qygg/` 与解析规则都复用 :class:`CsgGenericAdapter` ——
那是为总部站 `www.csg.cn` 量身写的（父类默认 `list_url` 就是该栏目）。

**为什么单独建源而不是并到 5 个省站**：
`articles.source_id` 唯一，一篇文章无法同时归属多省。
让 5 个省站都抓总部列表页会产出 5 份重复。
故这里作为**独立的集团级新闻源**，`province_code` 留空。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.csg_generic import CsgGenericAdapter


class CsgHqAdapter(CsgGenericAdapter):
    """中国南方电网有限责任公司（集团总部）。"""

    source_id = "csg_hq"
    source_name = "中国南方电网有限责任公司（集团总部）"
    province_code = ""
    category = "csg"
    priority = 1
    crawl_interval_hours = 2
    list_url = "https://www.csg.cn/xwzx/qygg/"
    column_url = "https://www.csg.cn/xwzx/qygg/"
    allowed_hosts = ("www.csg.cn",)
    language = "zh-CN"


register(CsgHqAdapter())
