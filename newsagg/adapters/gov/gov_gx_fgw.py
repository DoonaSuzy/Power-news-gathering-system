"""政府来源 —— 广西壮族自治区发展和改革委员会（gov_gx_fgw）。

由 :func: 按来源清单
动态生成，栏目路径取自 sources_seed.ADDITIONAL_SOURCES。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.gov_generic import build_gov_adapter

GovGxFgwAdapter = build_gov_adapter('gov_gx_fgw')

register(GovGxFgwAdapter())
