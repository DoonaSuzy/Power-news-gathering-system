"""金融数据爬虫：Ing 现价、CECI 进口煤指数、市场电价日报。

三个爬虫共享 :class:`BaseFinanceCrawler`：

- 会话（:class:`~newsagg.finance.auth.HttpSession`）、登录态管理
  （:class:`~newsagg.finance.auth.CookieTokenManager`，401/403 自动重新登录后
  由 :meth:`BaseFinanceCrawler._get` 自动重试一次）、邮件通知器与写入器均为
  可注入依赖，测试全程离线；
- ``collect()`` 走 parse_list → parse_detail 两段式：列表页定位详情链接，
  详情页解析为 :mod:`newsagg.finance.models` 中的模型行；
- ``run()`` 为带告警与远程恢复的执行入口：失败时经
  :class:`~newsagg.finance.alerting.AlertManager` 告警并下发恢复命令，
  成功后重试一次，最终通过写入器（``SqliteWriter`` 离线 / ``MySqlWriter`` 生产
  占位）落库。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import urljoin

from parsel import Selector

from newsagg.errors import PowerNewsError
from newsagg.finance.alerting import AlertManager
from newsagg.finance.auth import CookieTokenManager, HttpSession
from newsagg.finance.emailer import EmailNotifier
from newsagg.finance.models import CoalIndex, MarketPriceDaily, SpotPrice


class FinanceCrawlError(PowerNewsError):
    """金融爬虫抓取失败（HTTP 错误、解析失败等）。"""


@dataclass(frozen=True)
class SourceConfig:
    """单个金融数据源的配置。"""

    name: str
    category: str
    base_url: str
    list_path: str
    #: 采集间隔（秒）
    interval_seconds: int
    #: 落库表名
    table: str


@runtime_checkable
class Writer(Protocol):
    """行写入器协议：把模型行 dict 写入存储。"""

    def write(self, source: str, table: str, rows: list[dict[str, Any]]) -> int: ...


class SqliteWriter:
    """离线写入器：sqlite 落库 ``finance_rows`` 宽表（测试/本地开发用）。"""

    DDL = (
        "CREATE TABLE IF NOT EXISTS finance_rows ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " source TEXT NOT NULL,"
        " table_name TEXT NOT NULL,"
        " row_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(self.DDL)
        self._conn.commit()

    def write(self, source: str, table: str, rows: list[dict[str, Any]]) -> int:
        now = datetime.now(UTC).isoformat()
        self._conn.executemany(
            "INSERT INTO finance_rows (source, table_name, row_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            [
                (source, table, json.dumps(row, ensure_ascii=False, sort_keys=True), now)
                for row in rows
            ],
        )
        self._conn.commit()
        return len(rows)

    def count(self, source: str | None = None) -> int:
        if source is None:
            cursor = self._conn.execute("SELECT COUNT(*) FROM finance_rows")
        else:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM finance_rows WHERE source = ?", (source,)
            )
        return int(cursor.fetchone()[0])

    def fetch(self, source: str | None = None) -> list[dict[str, Any]]:
        """按来源取回全部行（反序列化 row_json，供测试断言）。"""
        if source is None:
            cursor = self._conn.execute(
                "SELECT source, row_json FROM finance_rows ORDER BY id"
            )
        else:
            cursor = self._conn.execute(
                "SELECT source, row_json FROM finance_rows WHERE source = ?"
                " ORDER BY id",
                (source,),
            )
        return [
            {"source": src, **json.loads(row_json)} for src, row_json in cursor.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()


class MySqlWriter:
    """MySQL 写入器占位（生产形态）。

    - 注入 ``cursor``（pymysql DictCursor 等）时执行 ``executemany`` 真实落库；
    - 不注入时仅内存记录，保证离线测试可运行——不发起任何数据库连接。
    """

    def __init__(self, *, dsn: str | None = None, cursor: Any | None = None) -> None:
        self.dsn = dsn
        self._cursor = cursor
        #: 内存记录（离线占位行为）。
        self.written: list[tuple[str, str, dict[str, Any]]] = []

    def write(self, source: str, table: str, rows: list[dict[str, Any]]) -> int:
        if self._cursor is not None:
            self._cursor.executemany(
                "INSERT INTO finance_rows (source, table_name, row_json)"
                " VALUES (%s, %s, %s)",
                [
                    (source, table, json.dumps(row, ensure_ascii=False))
                    for row in rows
                ],
            )
        self.written.extend((source, table, row) for row in rows)
        return len(rows)


def _to_float(text: Any) -> float | None:
    """从千分位/单位混杂的文本中提取浮点数；失败返回 None。"""
    if isinstance(text, (int, float)):
        return float(text)
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace("，", "").strip()
    for token in cleaned.split():
        try:
            return float(token)
        except ValueError:
            continue
    try:
        return float(cleaned)
    except ValueError:
        return None

class BaseFinanceCrawler:
    """金融爬虫基类：请求（含 401/403 自动重登重试）+ 两段式解析 + 落库。

    参数
    ----
    session:
        可注入 HTTP 会话（Fake 用于测试）。
    auth:
        :class:`CookieTokenManager`，负责凭据过期检测与自动重新登录。
    notifier / writer / alert_manager:
        均为可选注入：邮件通知器、行写入器、告警与远程恢复管理器。
    """

    #: 子类必须覆盖：数据源配置
    SOURCE: ClassVar[SourceConfig]
    #: 子类必须覆盖：``"html"`` 或 ``"json"``
    PAYLOAD: ClassVar[str]

    def __init__(
        self,
        *,
        session: HttpSession,
        auth: CookieTokenManager,
        notifier: EmailNotifier | None = None,
        writer: Writer | None = None,
        alert_manager: AlertManager | None = None,
    ) -> None:
        self.session = session
        self.auth = auth
        self.notifier = notifier
        self.writer = writer
        self.alert_manager = alert_manager
        #: 最近一次 run() 解析出的行（dict 形式）。
        self.last_run_rows: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 配置

    @property
    def source_name(self) -> str:
        return self.SOURCE.name

    def list_url(self) -> str:
        return self.SOURCE.base_url.rstrip("/") + self.SOURCE.list_path

    # ------------------------------------------------------------------ 抓取

    def _get(self, url: str, *, params: dict[str, Any] | None = None, depth: int = 0) -> Any:
        """带凭据的 GET：401/403 时自动重新登录并重试一次。"""
        self.auth.before_request()
        response = self.session.get(url, params=params, headers=self.auth.headers())
        if self.auth.handle_response(response) and depth == 0:
            return self._get(url, params=params, depth=1)
        if getattr(response, "status_code", 200) >= 400:
            raise FinanceCrawlError(
                f"{self.source_name} 请求失败 {url} -> HTTP {response.status_code}"
            )
        return response

    def fetch_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        return self._get(url, params=params).text

    def fetch_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._get(url, params=params).json()

    def _fetch_payload(self, url: str) -> Any:
        if self.PAYLOAD == "json":
            return self.fetch_json(url)
        return self.fetch_text(url)

    # ------------------------------------------------------------------ 解析

    def parse_list(self, payload: Any, *, url: str) -> list[dict[str, Any]]:
        """解析列表页，返回含 ``detail_url`` 的条目 dict 列表（子类实现）。"""
        raise NotImplementedError

    def parse_detail(
        self, payload: Any, *, url: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """解析详情页，返回模型行 dict 列表（子类实现）。"""
        raise NotImplementedError

    def collect(self) -> list[dict[str, Any]]:
        """列表页 → 详情页两段式采集，返回全部模型行。"""
        items = self.parse_list(self._fetch_payload(self.list_url()), url=self.list_url())
        rows: list[dict[str, Any]] = []
        for item in items:
            detail_url = item.get("detail_url")
            if not detail_url:
                continue
            payload = self._fetch_payload(detail_url)
            rows.extend(self.parse_detail(payload, url=detail_url, item=item))
        return rows

    # ------------------------------------------------------------------ 执行

    def run(self) -> int:
        """带告警 + 远程恢复的执行入口，返回写入行数。

        首次失败：经 AlertManager 告警并下发 restart 恢复命令，随后重试一次；
        重试仍失败则向上抛出（告警已记录）。
        """
        try:
            rows = self.collect()
        except Exception as exc:
            if self.alert_manager is None:
                raise
            self.alert_manager.on_failure(
                self.source_name,
                exc,
                detail={"list_url": self.list_url(), "attempt": 1},
            )
            self.alert_manager.remote_recover(
                self.source_name,
                action="restart",
                payload={"list_url": self.list_url(), "reason": str(exc)},
            )
            rows = self.collect()
        self.last_run_rows = rows
        if self.writer is not None:
            self.writer.write(self.source_name, self.SOURCE.table, rows)
        return len(rows)


class IngSpotPriceCrawler(BaseFinanceCrawler):
    """Ing 现价爬虫（HTML 站点，列表页定位、详情页取价）。"""

    SOURCE = SourceConfig(
        name="ing_spot_price",
        category="有色金属现价",
        base_url="https://www.ingmm.com",
        list_path="/price/spot.html",
        interval_seconds=3600,
        table="spot_prices",
    )
    PAYLOAD: ClassVar[str] = "html"

    def parse_list(self, payload: Any, *, url: str) -> list[dict[str, Any]]:
        selector = Selector(text=payload)
        items: list[dict[str, Any]] = []
        for row in selector.css("tr.row"):
            href = row.css("a.detail::attr(href)").get()
            name = (row.css("td.name::text").get() or "").strip()
            ts = (row.css("td.date::text").get() or "").strip()
            if not (href and name):
                continue
            items.append(
                {"detail_url": urljoin(url, href), "index_name": name, "ts": ts}
            )
        return items

    def parse_detail(
        self, payload: Any, *, url: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        selector = Selector(text=payload)
        name = (selector.css("div.spot-detail h1::text").get() or "").strip() or str(
            item.get("index_name", "")
        )
        price = _to_float(selector.css("div.spot-detail span.price::text").get())
        unit = (selector.css("div.spot-detail span.unit::text").get() or "").strip()
        ts = (selector.css("div.spot-detail span.ts::text").get() or "").strip() or str(
            item.get("ts", "")
        )
        if price is None or not name:
            return []
        row = SpotPrice(index_name=name, price=price, unit=unit, ts=ts)
        return [row.to_row()]


class CeciCoalIndexCrawler(BaseFinanceCrawler):
    """CECI 进口煤指数爬虫（JSON API，列表页给 id，详情页给数值）。"""

    SOURCE = SourceConfig(
        name="ceci_coal_index",
        category="进口煤指数",
        base_url="https://cec.org.cn",
        list_path="/ceci/api/import-coal-index.json",
        interval_seconds=86400,
        table="coal_indexes",
    )
    PAYLOAD: ClassVar[str] = "json"

    def parse_list(self, payload: Any, *, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for entry in (payload.get("data") or {}).get("items") or []:
            href = entry.get("url") or ""
            if not href:
                continue
            items.append(
                {
                    "detail_url": urljoin(url, href),
                    "name": str(entry.get("name") or ""),
                }
            )
        return items

    def parse_detail(
        self, payload: Any, *, url: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        data = payload.get("data") or {}
        value = _to_float(data.get("value"))
        if value is None:
            return []
        row = CoalIndex(
            name=str(data.get("name") or item.get("name") or ""),
            value=value,
            ts=str(data.get("ts") or ""),
            change=_to_float(data.get("change")) or 0.0,
        )
        return [row.to_row()]


class MarketPriceDailyCrawler(BaseFinanceCrawler):
    """市场电价日报爬虫（HTML 站点，列表页定位日报，详情页按地区取价）。"""

    SOURCE = SourceConfig(
        name="market_price_daily",
        category="市场电价日报",
        base_url="https://pm.gsjsgs.com",
        list_path="/market/daily.html",
        interval_seconds=86400,
        table="market_prices",
    )
    PAYLOAD: ClassVar[str] = "html"

    def parse_list(self, payload: Any, *, url: str) -> list[dict[str, Any]]:
        selector = Selector(text=payload)
        return [
            {"detail_url": urljoin(url, href)}
            for href in selector.css("ul.daily-list a::attr(href)").getall()
            if href
        ]

    def parse_detail(
        self, payload: Any, *, url: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        selector = Selector(text=payload)
        ts = (selector.css("span.report-date::text").get() or "").strip()
        rows: list[dict[str, Any]] = []
        for tr in selector.css("table.price-table tr"):
            region = (tr.css("td.region::text").get() or "").strip()
            price = _to_float(tr.css("td.price::text").get())
            if region and price is not None:
                rows.append(
                    MarketPriceDaily(region=region, price=price, ts=ts).to_row()
                )
        return rows


__all__ = [
    "BaseFinanceCrawler",
    "CeciCoalIndexCrawler",
    "FinanceCrawlError",
    "IngSpotPriceCrawler",
    "MarketPriceDailyCrawler",
    "MySqlWriter",
    "SourceConfig",
    "SqliteWriter",
    "Writer",
]

