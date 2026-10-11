"""HTTP 数据源：走数据中台的时序接口（``/api/v1/weather``、``/api/v1/market``）。

为什么要有它
------------
MySQL 源要求调用方持有库账号；中台化之后，业务侧（分析师的 Notebook、前端、
其它部门的脚本）应该只有一把 API Key。两条路径共用 :data:`CATEGORY_BINDINGS`
同一份落点表，也共用同一条省级汇总口径（圆均值 / MAX），所以同一个查询在
两边给出同一个数——**口径不统一比没有数据更危险**。

只依赖标准库
------------
用 ``urllib.request`` 而不是 requests/httpx：SDK 的依赖表越短，装进客户环境
失败的概率越低。需要连接复用时可以传 ``transport`` 注入自己的实现。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

import pandas as pd

from .config import Settings, get_settings
from .errors import (
    ConfigError,
    DataSourceError,
    UnknownRegionError,
    UnsupportedCategoryError,
)
from .pinyin import RegionIndex, default_region_index
from .timeseries import (
    CANONICAL_COLUMNS,
    CATEGORY_BINDINGS,
    MARKET_REGION_ALIASES,
    MARKET_REGION_LABELS,
    WEATHER_PARAM_TO_COLUMN,
    WEATHER_PRODUCTS,
    WEATHER_REGION_CODES,
    SourceBinding,
    _db_time,
    backed_categories,
    resolve_category,
    weather_column,
    weather_unit,
)

__all__ = ["HttpTimeSeriesSource"]

#: 单次 HTTP 请求的行数（服务端上限 50000，这里按页翻）
DEFAULT_PAGE_SIZE = 5000

#: 一次 fetch 最多拉多少行，防止时间窗写错把内存打满
DEFAULT_MAX_ROWS = 200_000


def _as_time(value: object, *, end_of_day: bool = False) -> str | None:
    """把用户写法转成服务端 ``parse_day`` 认得的格式。

    服务端只吃 ``%Y-%m-%d %H:%M:%S`` / ``%Y-%m-%d %H:%M`` / ``%Y-%m-%d``，
    **不吃 ISO 的 ``T`` 分隔符**——直接 ``isoformat()`` 会换来一个 400。
    秒精度取自 :func:`_db_time`，与 MySQL 源的区间上界是同一个值。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _db_time(value, end_of_day=end_of_day).strftime("%Y-%m-%d %H:%M:%S")


class HttpTimeSeriesSource:
    """中台时序接口的数据源实现（与 :class:`MySQLTimeSeriesSource` 同协议）。

    :param base_url: 形如 ``http://127.0.0.1:8000/api/v1``；缺省读配置
    :param token: Bearer Token，需要 ``weather:read`` / ``market:read`` 两个 scope
    :param product: 气象产品（``hist_rt`` / ``hist_fc`` / ``daily_fc``）
    :param level: ``province``=省内各站汇总（长表形状所需）；``station`` 会把
        同一省同一时刻塞进多行，与 SDK 的 ``[ts, region, value]`` 形状冲突，
        因此这里**只支持 province**，传别的直接报错而不是静默降级。
    :param transport: ``(url, headers) -> dict``，单测注入用
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        settings: Settings | None = None,
        product: str = "hist_rt",
        level: str = "province",
        page_size: int = DEFAULT_PAGE_SIZE,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout: float | None = None,
        region_index: RegionIndex | None = None,
        transport: Callable[[str, dict[str, str]], dict] | None = None,
    ) -> None:
        if product not in WEATHER_PRODUCTS:
            raise ValueError(
                f"未知气象产品 {product!r}，可选：{', '.join(WEATHER_PRODUCTS)}"
            )
        if level != "province":
            raise ValueError(
                "HttpTimeSeriesSource 只支持 level='province'：SDK 的长表形状要求"
                "「一个地区一条序列」，station 层级会让同省同时刻出现多行并被去重吞掉。"
                "需要站点明细请直接用 /api/v1/weather/{region}/{product}?level=station"
            )
        self.settings = settings or get_settings()
        self.base_url = str(base_url or self.settings.api_base_url or "").rstrip("/")
        self.token = token if token is not None else self.settings.api_token
        self.product = product
        self.level = level
        self.page_size = max(1, int(page_size))
        self.max_rows = max(1, int(max_rows))
        self.timeout = float(self.settings.api_timeout if timeout is None else timeout)
        self.region_index = region_index or default_region_index()
        self._transport = transport

    # ------------------------------------------------------------------ 传输
    def _url(self, path: str, params: dict[str, object]) -> str:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        encoded = urllib.parse.urlencode(query, doseq=True)
        return f"{self.base_url}{path}?{encoded}" if encoded else f"{self.base_url}{path}"

    def _get(self, path: str, params: dict[str, object]) -> dict:
        """一次 GET，拆掉统一响应信封，只返回 ``data``。

        失败必须**带上下文**地抛 :class:`DataSourceError`：只说「请求失败」的
        异常无法区分「Token 没权限」「大区名写错」「服务没起来」这三种常见情况。
        """
        if not self.base_url:
            raise ConfigError(
                "缺少中台地址：传入 base_url、设置 POWERSDK_API_BASE_URL，"
                "或改用 MySQLTimeSeriesSource 直连库"
            )
        url = self._url(path, params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        if self._transport is not None:
            payload = self._transport(url, headers)
        else:
            payload = self._urlopen(url, headers)

        code = int(payload.get("code", 0) or 0)
        if code != 0:
            raise DataSourceError(
                f"中台返回业务错误 code={code}：{payload.get('detail') or payload}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError(f"中台响应缺少 data 对象：{payload}")
        return data

    def _urlopen(self, url: str, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
            raise DataSourceError(f"HTTP {exc.code} {url}：{detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DataSourceError(f"无法访问中台 {self.base_url}：{exc}") from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"中台响应不是 JSON（{url}）：{body[:200]}") from exc
        if not isinstance(parsed, dict):
            raise DataSourceError(f"中台响应不是对象：{type(parsed).__name__}")
        return parsed

    def _fetch_all(self, path: str, params: dict[str, object]) -> tuple[list[dict], dict]:
        """按 ``offset`` 翻页取全量，返回 ``(items, meta)``。

        服务端 ``truncated`` 只在「正好取满 limit」时为真，所以终止条件用
        「本页不足一页」而不是读 truncated；``max_rows`` 是内存护栏，
        触顶时保留已取数据并在 meta 里标记，不静默截断。
        """
        items: list[dict] = []
        meta: dict = {}
        offset = 0
        while True:
            page = self._get(path, {**params, "limit": self.page_size, "offset": offset})
            meta = page.get("meta") or {}
            batch = page.get("items") or []
            items.extend(batch)
            if len(batch) < self.page_size:
                break
            if len(items) >= self.max_rows:
                meta = {**meta, "capped_by_client": True, "max_rows": self.max_rows}
                break
            offset += len(batch)
        return items, meta

    # ------------------------------------------------------------------ 取数
    def fetch(
        self,
        category: object,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "raw",
        param: str | None = None,
    ) -> pd.DataFrame:
        cat = resolve_category(category)
        binding = CATEGORY_BINDINGS[cat.value]
        if binding is None:
            raise UnsupportedCategoryError(
                cat.value, source="HTTP", available=backed_categories()
            )
        if binding.kind == "weather":
            return self._fetch_weather(region, start, end, granularity, param)
        return self._fetch_market(binding, region, start, end, param)

    def _fetch_weather(
        self,
        region: Sequence[str] | str | None,
        start: object,
        end: object,
        granularity: str,
        param: str | None,
    ) -> pd.DataFrame:
        column = weather_column(param)
        db_granularity = "daily" if str(granularity) == "daily" else "hourly"
        codes = self._province_codes(region)
        common = {
            "granularity": db_granularity,
            "params": column,
            "level": self.level,
            "province": ",".join(codes) if codes else None,
            "since": _as_time(start),
            "until": _as_time(end, end_of_day=True),
        }
        # 单位以 SDK 侧的列→单位表为准，不从响应 meta 里读：接口改版少返回
        # units 时，读 meta 会退化成「unit=None」，而单位错了量纲就全错了。
        unit = weather_unit(column, db_granularity)
        records: list[tuple] = []
        # 接口按「大区 × 产品」分表，省份落在哪个大区不由 SDK 猜：
        # 八个大区各问一次，带 province 过滤时不匹配的那几张返回空行，
        # 代价是几次空查询，换来的是「省份→大区归属」只有库里一个真源。
        for region_name in WEATHER_REGION_CODES:
            items, _meta = self._fetch_all(f"/weather/{region_name}/{self.product}", common)
            for item in items:
                records.append(
                    (
                        item.get("ts"),
                        self._province_name(item.get("province_code")),
                        item.get(column),
                        unit,
                    )
                )
        frame = pd.DataFrame(records, columns=list(CANONICAL_COLUMNS))
        return frame.sort_values(["region", "ts"], kind="stable").reset_index(drop=True)

    def _fetch_market(
        self,
        binding: SourceBinding,
        region: Sequence[str] | str | None,
        start: object,
        end: object,
        param: str | None,
    ) -> pd.DataFrame:
        if not binding.dataset:
            raise DataSourceError(f"绑定 {binding.kind} 缺少数据集")
        grade = param or binding.default_grade
        items, _meta = self._fetch_all(
            f"/market/{binding.dataset}",
            {
                "region": ",".join(self._market_regions(region) or []) or None,
                "grade": grade,
                "since": _as_time(start),
                "until": _as_time(end, end_of_day=True),
            },
        )
        records = [
            (item.get("price_date"), item.get("region"), item.get("value"), item.get("unit"))
            for item in items
        ]
        frame = pd.DataFrame(records, columns=list(CANONICAL_COLUMNS))
        return frame.sort_values(["region", "ts"], kind="stable").reset_index(drop=True)

    # ------------------------------------------------------------------ 地区
    def _province_codes(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        records = self.region_index.resolve_many(region)
        return None if records is None else tuple(record.code for record in records)

    def _province_name(self, code: object) -> str:
        record = self.region_index.lookup(str(code))
        if record is None:
            raise DataSourceError(f"行政区划码 {code!r} 不在 SDK 地区索引里")
        return record.name

    def _market_regions(self, region: Sequence[str] | str | None) -> tuple[str, ...] | None:
        queries = [region] if isinstance(region, str) else list(region or [])
        if not queries:
            return None
        names: list[str] = []
        unknown: list[str] = []
        for item in queries:
            record = self.region_index.lookup(item)
            if record is not None:
                names.append(record.name)
                continue
            text = str(item).strip()
            alias = MARKET_REGION_ALIASES.get(text.lower())
            if alias is None and text in MARKET_REGION_LABELS:
                alias = text
            if alias is None:
                unknown.append(text)
                continue
            names.append(alias)
        if unknown:
            raise UnknownRegionError(", ".join(unknown), self.region_index.names)
        return tuple(dict.fromkeys(names))

    # ------------------------------------------------------------------ 元信息
    def catalog(self) -> dict[str, object]:
        """当前配置下的能力清单（哪些类别有数、走的是哪个产品）。"""
        return {
            "base_url": self.base_url,
            "product": self.product,
            "level": self.level,
            "backed_categories": backed_categories(),
            "weather_params": tuple(sorted(set(WEATHER_PARAM_TO_COLUMN.values()))),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"HttpTimeSeriesSource(base_url={self.base_url!r}, product={self.product!r})"
