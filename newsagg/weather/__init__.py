"""气象数据采集子包。

分层与 :mod:`newsagg.market` 保持一致：

- :mod:`newsagg.weather.models` —— 站点/变量/表命名的**单一事实来源**；
- :mod:`newsagg.weather.client` —— Open-Meteo 取数与解析（纯函数，可离线单测）；
- :mod:`newsagg.weather.store`  —— 按大区路由、幂等写入 24 张表；
- :mod:`newsagg.weather.schedule` —— 每日滚动采集的编排；
- :mod:`newsagg.weather.cli`    —— 命令行入口。

24 张表 = **8 大区（华北/东北/华东/华中/华南/西南/西北/港澳台） × 3 数据产品**
（``hist_rt`` 历史实时 / ``hist_fc`` 历史预测 / ``daily_fc`` 每日预测）。
建表 SQL 由 ``scripts/gen_weather_schema.py`` 从 ``models`` 派生，勿手改。
"""

from __future__ import annotations

__all__ = ["models", "client", "store", "schedule", "cli"]
