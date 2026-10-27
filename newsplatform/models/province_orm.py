"""省级新闻库（``newsagg_platform``）的**只读** ORM 映射。

为什么必须单独一个 ``DeclarativeBase``
--------------------------------------
采集库（``powernews``）里同样有 ``articles`` / ``sources`` / ``provinces``
三张表，但列名与语义都不同：

======================  ==============================  ==========================
语义                    电力（powernews）               省级（newsagg_platform）
======================  ==============================  ==========================
站点名                  ``sources.name``                ``sources.media_name``
列表页                  ``sources.list_url``            ``sources.column_url``
状态                    ``status='active'`` + bool       ``status='enabled'``
分类                    ``category``                    （无）
======================  ==============================  ==========================

同一份 ``MetaData`` 里挂两张同名表会直接抛
``InvalidRequestError: Table 'articles' is already defined``，所以省级库必须
用自己的基类。**这些表只映射、不建表**——采集与写入永远归省级那套
``newsagg``，中台只读，和电力侧对 ``collector`` 库的约定一致。

字段口径提醒
------------
省级库的 ``url_hash`` 是 **64 位（SHA-256）**，电力库是 **40 位（SHA-1）**。
两者不能直接比较，因此跨域合并时**不做 URL 级去重**，只按
``(channel, id)`` 标识一行。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["ProvinceBase", "ProvinceArticle", "ProvinceSource", "ProvinceDict"]


class ProvinceBase(DeclarativeBase):
    """省级新闻库的只读 ORM 基类（不参与建表）。"""


class ProvinceDict(ProvinceBase):
    """省级行政区字典（31 个大陆省级行政区）。"""

    __tablename__ = "provinces"

    # 采集库是 CHAR(2)：'44' 表示广东。与电力库的 6 位码不同，
    # 所以仓储层必须做一次归一（见 province_articles.code_variants）
    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)


class ProvinceSource(ProvinceBase):
    """省级新闻来源（一省一源，共 34 条）。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    adapter_name: Mapped[str] = mapped_column(String(64), nullable=False)
    media_name: Mapped[str] = mapped_column(String(128), nullable=False)
    province_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    column_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 采集库用 verification_status（candidate/verified/enabled/paused），
    # 平台库才叫 status。合并后接的是采集库，所以这里必须用前者——
    # 映射错了会在查询时报 1054 Unknown column，而不是空结果，反而好排查。
    verification_status: Mapped[str] = mapped_column(String(16), nullable=False, default="enabled")
    cooldown_until_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_discovery_success_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_full_rescan_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProvinceArticle(ProvinceBase):
    """省级新闻文章。

    列名与电力库**几乎逐字相同**（这是两库能合并查询的基础），
    差异只有 ``url_hash`` / ``content_hash`` 的长度和 ``quality_flags`` 的默认值。
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    published_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at_cst: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    time_precision: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 采集库里是 JSON 列（不是 VARCHAR）：按 Text 读，再在仓储层归一成字符串，
    # 避免 SQLAlchemy 反序列化成 list 后与 ArticleListItem.quality_flags: str 冲突。
    quality_flags: Mapped[str] = mapped_column(Text, nullable=True)
    # 注意：采集库**没有** created_at，只有 discovered_at_utc
    discovered_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
