"""数据中台（``newsplatform``）—— 电力新闻聚合项目的对外服务层。

定位
----
采集层（``newsagg``）负责「把 34 个省级行政区的电力新闻抓回来」，
数据中台负责「把抓回来的数据稳定地交付出去」：查询、统计、来源管理、
健康检查、OpenAPI 文档。

与采集层的边界（改造方案 §1.4 / §2.3）
--------------------------------------
- 采集层**不知道** Web 框架的存在：``newsagg`` 不 import FastAPI；
- 中台**不写**采集库：对采集库只读（``collector_engine``），
  自己的状态（API Key、审计日志、统计快照）写在独立的
  ``powernews_platform`` 库（``platform_engine``）；
- 两者共用的只有 ``.env`` 里的连接信息与 ``sql/`` 下的 DDL。

分层
----
``api/v1`` 只做 HTTP 编排 → ``services`` 承载业务规则 →
``repositories`` 落 SQL（async SQLAlchemy 2.0）→ ``models`` 定义 ORM 与 Schema。
依赖方向单向向下，反向依赖一律通过 FastAPI ``Depends`` 注入。

（本包为全新搭建，未复用旧项目的 Flask 管理后台——改造方案 §2 的目标态就是
「FastAPI 单框架」，因此这里不存在需要渐进迁移的 Flask 代码。）
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
