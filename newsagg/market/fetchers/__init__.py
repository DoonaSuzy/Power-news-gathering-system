"""采集器注册表。

新增一个数据源时：写一个 :class:`~newsagg.market.fetchers.base.MarketFetcher`
子类，然后在这里登记 dataset → 类。CLI 与调度都从这里取，不必改别处。

注意：**多个 dataset 可以指向同一个采集器类**：

- ``ceci_import`` / ``ceci_pmi`` → ``CECIFetcher``（一份 CECI 周报同时发布两个指数）
- ``lng_spot`` / ``lng_signal`` → ``PPIFetcher``（一页生意社列表同时给出基准价与均差）
- ``lng_quote`` / ``lng_spot`` → ``SHFGXFetcher``（一次挂牌明细同时给出明细与汇总口径）

拆成两个类只会让请求数翻倍。CLI 侧按**类**去重，所以 ``--dataset all``
也只会各抓一次。

注意 ``lng_spot`` 有**三个**来源（``cqpgx`` / ``100ppi`` / ``shpgx``），
唯一键里的 ``source`` 让它们各自成行、互不覆盖。
"""

from newsagg.market.fetchers.base import MarketFetcher
from newsagg.market.fetchers.ceci_cec import CECIFetcher
from newsagg.market.fetchers.lng_100ppi import PPIFetcher
from newsagg.market.fetchers.lng_cqpgx import CLPFetcher
from newsagg.market.fetchers.lng_shpgx import SHFGXFetcher
from newsagg.market.fetchers.power_gzpec import GZPECFetcher

#: dataset → 采集器类
FETCHERS: dict[str, type[MarketFetcher]] = {
    "lng_spot": CLPFetcher,
    "lng_quote": SHFGXFetcher,
    "lng_signal": PPIFetcher,
    "ceci_import": CECIFetcher,
    "ceci_pmi": CECIFetcher,
    "power_spot": GZPECFetcher,
}

#: 一个采集器类可能产出**多个** dataset，但它的 ``dataset`` 属性只能取一个值。
#: 这张表写明每个类的**主数据集**，供测试断言「登记的值没有错位」——
#: 比如 ``PPIFetcher`` 的主数据集是 ``lng_spot`` 而不是 ``lng_signal``。
PRIMARY_DATASET: dict[str, str] = {
    "CLPFetcher": "lng_spot",
    "PPIFetcher": "lng_spot",
    "SHFGXFetcher": "lng_quote",
    "CECIFetcher": "ceci_import",
    "GZPECFetcher": "power_spot",
}

#: 采集器类 → 它**实际会产出的全部** dataset（含搭车产出的）。
#:
#: 为什么不能只靠 :data:`FETCHERS` 反查：那张表是 dataset → 类，一个 dataset
#: 只登记一个类。于是 ``--dataset lng_spot`` 只会跑 ``CLPFetcher``，
#: 把生意社与上海挂牌的现价**悄悄漏掉**——命令返回 0、日志也正常，
#: 只有翻库才发现少了两家。这张表把「谁还产出这个数据集」写成显式事实。
#:
#: 约束（有测试钉着）：键覆盖 ``FETCHERS`` 的每个类；值里每一项都在
#: :data:`~newsagg.market.models.DATASETS` 里；``PRIMARY_DATASET`` 是它的子集。
PRODUCED_DATASETS: dict[str, tuple[str, ...]] = {
    "CLPFetcher": ("lng_spot",),
    "PPIFetcher": ("lng_spot", "lng_signal"),
    "SHFGXFetcher": ("lng_quote", "lng_spot"),
    "CECIFetcher": ("ceci_import", "ceci_pmi"),
    "GZPECFetcher": ("power_spot",),
}

__all__ = [
    "FETCHERS",
    "PRIMARY_DATASET",
    "PRODUCED_DATASETS",
    "CECIFetcher",
    "CLPFetcher",
    "GZPECFetcher",
    "MarketFetcher",
    "PPIFetcher",
    "SHFGXFetcher",
]
