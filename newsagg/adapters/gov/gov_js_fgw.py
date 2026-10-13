"""政府来源 —— 江苏省发展和改革委员会（gov_js_fgw）。

由 `sources_seed.ADDITIONAL_SOURCES` 驱动，栏目路径实测取自该站首页。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.gov_generic import build_gov_adapter

GovJsFgwAdapter = build_gov_adapter('gov_js_fgw')

register(GovJsFgwAdapter())
