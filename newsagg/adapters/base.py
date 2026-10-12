"""适配器基类与数据契约（改造方案 §3.2）。

多源架构：从「每省 1 源」升级为「每省 N 源」。

    newsagg/adapters/
    ├── base.py            # 本文件：BaseAdapter + 数据契约
    ├── registry.py        # 适配器注册中心
    ├── _common.py         # 通用启发式解析（正文/标题/来源/时间选择器 + 回退）
    ├── listing.py         # 列表页链接抽取与分页
    ├── sgcc_generic.py    # 国家电网/南方电网站群的通用适配器
    ├── _gne.py            # GNE 通用正文抽取 fallback
    ├── beijing/
    │   └── sgcc_bj.py     # 一源一文件
    └── ...

每个适配器实现统一接口（``parse_list`` / ``parse_detail``），未编写专用适配器的
来源由 :class:`~newsagg.adapters.sgcc_generic.SgccGenericAdapter` +
:mod:`newsagg.adapters._gne` 兜底。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = [
    "ListItem",
    "ListParse",
    "DetailParse",
    "BaseAdapter",
    "AdapterContext",
]


@dataclass(slots=True)
class ListItem:
    """列表页中的一条候选。"""

    url: str
    title: str = ""
    list_time_text: str = ""


@dataclass(slots=True)
class ListParse:
    """列表页解析结果。"""

    items: list[ListItem] = field(default_factory=list)
    #: 下一页 URL（无则 None）
    next_url: str | None = None
    #: 列表是否按时间倒序（False 时蜘蛛会停止抓取以保护窗口语义）
    descending: bool = True


@dataclass(slots=True)
class DetailParse:
    """详情页解析结果。"""

    title: str = ""
    paragraphs: list[str] = field(default_factory=list)
    source_name: str = ""
    published_text: str = ""
    #: 正文区只有视频/图片，无文字（视频稿属正常，不计为解析失败）
    media_only: bool = False
    #: 是否由 GNE 通用抽取兜底产出（便于统计专用适配器的覆盖率）
    used_fallback: bool = False


@dataclass(slots=True)
class AdapterContext:
    """适配器元信息（写库与日志用）。"""

    source_id: str
    name: str
    province_code: str | None = None
    category: str = ""
    priority: int = 0


class BaseAdapter(ABC):
    """适配器统一接口。

    子类至少要实现 :meth:`parse_list` 与 :meth:`parse_detail`；
    ``allowed_hosts`` / ``list_url`` 用于收敛抓取范围。
    """

    #: 来源 id（与 ``sources_seed.SOURCES[].id`` 一致）
    source_id: str = ""
    #: 省份行政区划代码
    province_code: str | None = None
    #: 来源类别：sgcc / csg / independent / hmt / industry
    category: str = ""
    #: 来源显示名
    source_name: str = ""
    #: 采集优先级（0 最高）
    priority: int = 0
    #: 采集间隔（小时）
    crawl_interval_hours: int = 1

    #: 允许抓取的主机
    allowed_hosts: tuple[str, ...] = ()
    #: 列表页 URL
    list_url: str = ""
    #: 兼容别名（引擎读取 ``column_url``）
    column_url: str = ""
    #: 同站其他栏目页（多入口，提升覆盖）
    extra_column_urls: tuple[str, ...] = ()
    #: 该来源的**候选列表页**（按优先级，靠前的先试）。
    #:
    #: 为什么需要它：站群改版会让 `list_url` 静默失效 ——
    #: 江苏 `sgcc_js` 的声明路径 `/p1/` 实测返回 **404**，
    #: 改用 `/xwzx/` 后成功入库 **40 篇**；辽宁等站同理。
    #: 这里列出的地址会被 spider 依次尝试，首个能解析出条目的胜出。
    #:
    #: 与 ``extra_column_urls`` 的区别：后者是**并列的多栏目**（都要抓），
    #: 本字段是**同一栏目的备选路径**（互斥，只取第一个可用的）。
    candidate_list_urls: tuple[str, ...] = ()
    #: 单来源请求间隔（秒）；敏感站覆写为 5
    request_interval: float = 0.0
    #: 该来源专用 TLS 套件（OpenSSL 语法）；None 表示用默认
    tls_ciphers: str | None = None
    #: robots 策略：obey / ignore / custom
    robots_policy: str = "obey"
    #: 是否允许 GNE 兜底
    allow_gne_fallback: bool = True
    #: 内容语言（港澳台站点为繁体中文）
    language: str = "zh-CN"

    # ------------------------------------------------------------------
    @abstractmethod
    def parse_list(self, html: str, *, base_url: str) -> ListParse:
        """解析列表页。"""

    @abstractmethod
    def parse_detail(self, html: str, *, url: str) -> DetailParse:
        """解析详情页。"""

    # ------------------------------------------------------------------
    def allows(self, url: str) -> bool:
        """URL 是否属于本来源允许抓取的范围。"""
        from newsagg.urls import host_of

        host = host_of(url)
        if not host:
            return False
        if not self.allowed_hosts:
            return True
        for allowed in self.allowed_hosts:
            allowed = allowed.lower()
            if host == allowed or host.endswith("." + allowed):
                return True
            # www.x.y 派生出父域 x.y，放行 news./china./hn. 等子域
            parts = allowed.split(".")
            if len(parts) >= 3 and parts[0] == "www":
                parent = ".".join(parts[1:])
                if host == parent or host.endswith("." + parent):
                    return True
        return False

    def is_article(self, url: str) -> bool:
        """URL 是否为文章详情页（子类可覆写）。"""
        return self.allows(url) and bool(url)

    def robots_allowed(self, url: str) -> bool:
        """``robots_policy == "custom"`` 时的自定义判定；默认放行。"""
        return True

    def context(self) -> AdapterContext:
        return AdapterContext(
            source_id=self.source_id,
            name=self.source_name or self.source_id,
            province_code=self.province_code,
            category=self.category,
            priority=self.priority,
        )

    def describe(self) -> dict:
        """适配器自描述（用于 ``powernews adapters`` 命令）。"""
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "province_code": self.province_code,
            "category": self.category,
            "priority": self.priority,
            "interval_hours": self.crawl_interval_hours,
            "list_url": self.list_url or self.column_url,
            "extra_column_urls": len(self.extra_column_urls),
            "hosts": len(self.allowed_hosts),
            "robots_policy": self.robots_policy,
        }
