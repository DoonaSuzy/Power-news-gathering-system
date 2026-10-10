"""政府来源 —— 湖北省能源局（gov_hb_nyj）。

由 :func: 按来源清单
动态生成，栏目路径取自 sources_seed.ADDITIONAL_SOURCES。
"""

from __future__ import annotations

from newsagg.adapters import register
from newsagg.adapters.gov_generic import build_gov_adapter

GovHbNyjAdapter = build_gov_adapter('gov_hb_nyj')

register(GovHbNyjAdapter())
