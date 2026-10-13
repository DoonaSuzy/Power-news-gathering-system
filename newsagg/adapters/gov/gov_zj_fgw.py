"""政府来源 —— 浙江省发展和改革委员会（gov_zj_fgw）。

由 `sources_seed.ADDITIONAL_SOURCES` 驱动，栏目路径实测取自该站首页。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.gov_generic import build_gov_adapter

GovZjFgwAdapter = build_gov_adapter('gov_zj_fgw')

register(GovZjFgwAdapter())
