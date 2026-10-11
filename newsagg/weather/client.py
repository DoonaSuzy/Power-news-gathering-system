"""Open-Meteo HTTP 客户端：批量取数 + 时间切片 + 限速 + 解析。

职责边界（与 ``market`` 包同构）
-------------------------------
本模块只做「发请求 + 把 JSON 变成 :class:`~newsagg.weather.models.WeatherPoint`」，
**不碰数据库**。解析部分是纯函数（:func:`parse_payload`），喂一段写死的 JSON
就能单测，不需要起库、不需要联网。

三个产品的时间参数为什么不统一
------------------------------
- ``date_range``（历史实时 / 历史预测）：用 ``start_date``/``end_date`` 闭区间。
- ``rolling``（每日预测）：用 ``past_days``/``forecast_days``。预报接口虽然也认
  ``start_date``，但它只能落在这两个窗口拼出来的区间内，直接传日期反而容易 400。

限速与重试
----------
免费档额度是 **600 次/分 · 5,000 次/时 · 10,000 次/日**。这里的
:class:`RateLimiter` 是**本地保守闸门**，不依赖对方返回的限流头（对方目前不回
``Retry-After``）：一旦本进程触到 429，说明估算落后于实际，此时退避重试，
连续失败则中止本轮回填而不是硬打。
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from newsagg.logutil import get_logger
from newsagg.weather.models import (
    DAILY_AGGREGATION,
    ENDPOINTS,
    HOURLY_VARS,
    PRODUCT_SOURCE,
    RADIATION_HOUR_TO_MJ,
    VALUE_COLUMNS,
    WeatherPoint,
    chunk_periods,
    estimate_call_cost,
)

__all__ = [
    "Station",
    "RateLimiter",
    "FetchStats",
    "build_params",
    "parse_payload",
    "aggregate_daily",
    "dominant_wind_direction",
    "classify_rate_limit",
    "error_reason",
    "WeatherClient",
    "DEFAULT_UA",
]

log = get_logger("weather.client")

DEFAULT_UA = (
    "powernews-weather/0.1 (research aggregator; "
    "+https://github.com/DoonaSuzy/Power-news-gathering-system)"
)

#: 单请求默认站点数。**不是越大越好**：500 站 × 14 天 × 10 变量 ≈ 15 MB，
#: 对方生成要数分钟，中途任何抖动都会让整批作废。100 是本项目验证过的
#: 「单请求 20–40 秒、失败重试代价可接受」的折中值，可用 ``--batch`` 覆盖。
DEFAULT_BATCH = 100

#: 单请求超时（秒）。跨度大 + 站点多的请求对方需要现场聚合，给足时间。
DEFAULT_TIMEOUT = 240.0

#: 单次请求允许的**累计**等待上限（秒）。触到「小时/日配额用满」时正确做法是
#: 睡到窗口翻页，但不能让一个请求把进程无限挂住：超过这个上限就判定本轮跑不完，
#: 抛错交给上层把当前片记成 failed，下次续跑接着来。
DEFAULT_MAX_WAIT = 3 * 3600.0

#: Open-Meteo 的 429 文案 → 配额窗口键名。
#:
#: 写死对照表而不是靠猜：实测对方返回的 ``reason`` 是完整句子
#: （``"Hourly API request limit exceeded. Please try again in the next hour."``），
#: 只有认出是哪个窗口用满，才能决定「等 1 分钟 / 等 1 小时 / 今天放弃」。
#: 认不出来时返回 ``None``，退化为普通退避重试。
RATE_LIMIT_TOKENS: tuple[tuple[str, str], ...] = (
    ("minute", "minute"),
    ("hourly", "hour"),
    ("daily", "day"),
    ("monthly", "month"),
)


def classify_rate_limit(reason: str) -> str | None:
    """把 429 的 ``reason`` 文案映射到配额窗口键名（``minute``/``hour``/``day``/``month``）。

    纯函数，便于单测。认不出来返回 ``None``。
    """
    text = (reason or "").lower()
    for token, kind in RATE_LIMIT_TOKENS:
        if token in text:
            return kind
    return None


def error_reason(payload: Any) -> str:
    """从响应体里取出 ``reason``。对方的错误体是 ``{"error": true, "reason": "..."}``。

    解析不出来返回空串——调用方据此退化为「未知原因的 429」，不要因为解析
    失败就把整片数据判死。
    """
    if isinstance(payload, dict):
        raw = payload.get("reason")
        if isinstance(raw, str):
            return raw
    return ""


# --------------------------------------------------------------------------- 站点


@dataclass(frozen=True, slots=True)
class Station:
    """取数用的站点（站点主表里的一行）。"""

    station_id: str
    adcode: int
    lon: float
    lat: float
    province_code: str


# --------------------------------------------------------------------------- 限速


class RateLimiter:
    """滑动窗口限速 + 最小间隔。

    等待时长是**算出来的**而不是睡一个固定周期：窗口超限时直接等待
    「使配额回落所需的最早那个时间戳」过期。若每轮固定睡满一个窗口，
    同一窗口内会多睡一次（实测多等 60 秒），在数千次请求的回填里会白白
    拖长数小时。
    """

    #: ``(窗口秒数, 配额上限属性名)``
    WINDOWS: tuple[tuple[float, str], ...] = (
        (60.0, "per_minute"),
        (3_600.0, "per_hour"),
        (86_400.0, "per_day"),
    )

    #: 窗口键名 → ``(窗口秒数, 配额上限属性名)``。供 :meth:`block_until` 使用，
    #: 比 :data:`WINDOWS` 多一个 ``month``（月额度只用于「今天别跑了」的判断，
    #: 不参与每次请求的预检，否则 30 万额度会让预检失去意义）。
    QUOTA_WINDOWS: dict[str, tuple[float, str]] = {
        "minute": (60.0, "per_minute"),
        "hour": (3_600.0, "per_hour"),
        "day": (86_400.0, "per_day"),
        "month": (2_592_000.0, "per_month"),
    }

    def __init__(
        self,
        *,
        per_minute: int = 600,
        per_hour: int = 5_000,
        per_day: int = 10_000,
        per_month: int = 300_000,
        min_gap: float = 0.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.per_day = per_day
        self.per_month = per_month
        self.min_gap = min_gap
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._last: float | None = None
        self.waits = 0.0

    def _prune(self, now: float) -> None:
        """丢掉已经落到最大窗口之外的记录。"""
        horizon = now - 86_400.0
        while self._stamps and self._stamps[0][0] <= horizon:
            self._stamps.popleft()

    def _entries_within(self, now: float, window: float) -> list[tuple[float, float]]:
        """取窗口内的记录。**左开右闭**——正好卡在边界上的那次应当已过期。"""
        cutoff = now - window
        return [(t, w) for t, w in self._stamps if t > cutoff]

    def _wait_for(self, now: float, window: float, limit: int, weight: float) -> float:
        """判断本次（权重 ``weight``）能否落在窗口配额内，否则返回需等待秒数。

        **必须把本次的权重算进去再比较**：只在「已占用额度」上判断，
        会让最后一次请求总是溜过去（实测：3×170=510 < 600 → 第 4 次放行，
        实际发出后就是 680，直接 429）。

        一次请求可能消耗远大于 1 的权重（批量 100 站点 ≈ 170），
        所以这里按权重而非请求条数核算——只数请求条数会让限速形同虚设。
        """
        hits = self._entries_within(now, window)
        total = sum(w for _, w in hits)
        if total + weight <= limit:
            return 0.0
        need = total + weight - limit  # 至少要释放这么多权重才够放
        acc = 0.0
        for ts, w in hits:  # 按时间升序，先过期的先释放
            acc += w
            if acc >= need:
                return max(0.001, ts + window - now)
        return 0.0

    def acquire(self, weight: float = 1.0) -> float:
        """按权重占用配额，必要时休眠，返回本次实际等待秒数。

        Args:
            weight: 本次请求折算的调用次数，由
                :func:`newsagg.weather.models.estimate_call_cost` 得出。
        """
        if weight <= 0:
            raise ValueError("weight 必须为正")
        waited = 0.0
        while True:
            now = self._clock()
            self._prune(now)

            waits = [
                self._wait_for(now, window, getattr(self, attr), weight)
                for window, attr in self.WINDOWS
            ]
            if self._last is not None and self.min_gap > 0:
                gap = self._last + self.min_gap - now
                if gap > 0:
                    waits.append(gap)

            pending = [w for w in waits if w > 0]
            if not pending:
                self._stamps.append((now, weight))
                self._last = now
                return waited

            sleep = min(pending)
            self.waits += sleep
            waited += sleep
            time.sleep(sleep)

    def block_until(self, kind: str, now: float | None = None) -> float:
        """把某个配额窗口**记成已用满**，返回预计需要等待的秒数（不实际睡眠）。

        为什么需要它：本地估重永远落后于服务端实际计费，真触到 429 时对方会
        在 ``reason`` 里写明是哪个窗口用满。此时在同一窗口内退避重试是纯浪费
        ——实测连续 4 次都是同一个 429，最终整片被记成 failed。正确做法是让
        本地闸门也认定该窗口已满，于是下一次 :meth:`acquire` 会自然睡到窗口
        翻页再继续，这一片也就不用重跑。

        Args:
            kind: ``minute`` / ``hour`` / ``day`` / ``month``。
            now: 仅测试用，注入时钟读数。

        Returns:
            预计等待秒数（约等于该窗口长度）。
        """
        window_attr = self.QUOTA_WINDOWS.get(kind)
        if window_attr is None:
            raise ValueError(f"未知配额窗口 {kind!r}，可选 {sorted(self.QUOTA_WINDOWS)}")
        window, attr = window_attr
        stamp = self._clock() if now is None else now
        self._stamps.append((stamp, float(getattr(self, attr))))
        self._last = stamp
        return window


# --------------------------------------------------------------------------- 统计


@dataclass(slots=True)
class FetchStats:
    """一轮取数的统计。"""

    requests: int = 0
    retries: int = 0
    failed: int = 0
    points_hourly: int = 0
    points_daily: int = 0
    cost: float = 0.0
    waited: float = 0.0
    per_table: dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        return (
            f"请求 {self.requests} 次（重试 {self.retries}、失败 {self.failed}）"
            f"｜逐时 {self.points_hourly} 行 / 逐日 {self.points_daily} 行"
            f"｜折算调用 {self.cost:.1f} 次｜限速等待 {self.waited:.0f}s"
        )


# --------------------------------------------------------------------------- 参数


def _coord(value: float) -> str:
    """坐标格式化：保留 6 位小数并去掉无意义的尾零。

    **不要用 ``f"{v:g}"``**：``%g`` 默认只保留 6 位**有效数字**，
    会把 ``116.4167`` 截成 ``116.417``（约 30 m 偏差），
    也会把 ``121.4844`` 写成 ``121.484``。坐标截断不会报错，
    只会让请求的点悄悄偏离站点几十一百米。
    """
    text = repr(round(float(value), 6))
    return text[:-2] if text.endswith(".0") else text


def build_params(
    *,
    product: str,
    stations: Sequence[Station],
    start: date,
    end: date,
    today: date | None = None,
) -> dict[str, str]:
    """构造查询参数。多地点用逗号拼接（接口按同序返回数组）。"""
    if product not in ENDPOINTS:
        raise ValueError(f"未知产品 {product!r}")

    _, style = ENDPOINTS[product]
    params: dict[str, str] = {
        "latitude": ",".join(_coord(s.lat) for s in stations),
        "longitude": ",".join(_coord(s.lon) for s in stations),
        # 只请求逐时：变量数 10 → 计费倍数 1.0。请求 daily 会让变量数变 17、
        # 倍数 1.7，每日增量 14,662 权重直接超出免费档 10,000/日。
        # 逐日行改由 :func:`aggregate_daily` 本地算，口径见 models.DAILY_AGGREGATION。
        "hourly": ",".join(v[0] for v in HOURLY_VARS),
        "timezone": "Asia/Shanghai",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    if style == "date_range":
        params["start_date"] = start.isoformat()
        params["end_date"] = end.isoformat()
    else:
        today = today or date.today()
        past = max(0, (today - start).days)
        future = max(1, (end - today).days + 1)
        if past > 92:
            raise ValueError(
                f"滚动预报接口的 past_days 上限 92，本次需要 {past} 天；"
                "更早的历史请改用 hist_rt / hist_fc 产品"
            )
        if future > 16:
            raise ValueError(
                f"滚动预报接口的 forecast_days 上限 16，本次需要 {future} 天"
            )
        params["past_days"] = str(past)
        params["forecast_days"] = str(future)
    return params


# --------------------------------------------------------------------------- 解析


def _to_datetime(text: str) -> datetime:
    """``2026-01-01T13:00`` → ``datetime``。接口在极少数情况下回 ``T00:00:00``。"""
    return datetime.fromisoformat(text.replace("Z", ""))


def dominant_wind_direction(pairs: Iterable[tuple[float, float]]) -> float | None:
    """风速加权的圆均值（度），``pairs`` 为 ``(风向°, 风速)``。

    风向是角度量，算术平均会在 0°/360° 交界处算反——库内实测出现过
    「接口 243°、简单均值 96°」。按风速加权的单位向量平均才能复现接口的
    ``wind_direction_10m_dominant``（5,866 个 station-day 里 99.97% 落在 ±5°）。
    静风（权重和为 0）返回 ``None``，让下游写 NULL 而不是写一个假方向。
    """
    x = y = 0.0
    for direction, speed in pairs:
        rad = math.radians(direction)
        x += speed * math.cos(rad)
        y += speed * math.sin(rad)
    if x == 0.0 and y == 0.0:
        return None
    degrees = math.degrees(math.atan2(y, x)) % 360.0
    # 北向风在浮点下会得到 -1e-15°，取模后正好等于 360.0；写进 SMALLINT UNSIGNED
    # 列会与接口的 0° 相差 360，所以把值域归一到 [0, 360)。
    return 0.0 if degrees >= 360.0 else degrees


def aggregate_daily(
    times: Sequence[str],
    series_by_col: dict[str, Sequence[Any]],
) -> dict[date, dict[str, float]]:
    """由同一次响应的逐时序列本地算逐日值：``日期 → 列名 → 值``。

    缺测（``None``）跳过、**不当 0 算**：湿度缺 3 小时若按 0 计入，日均值会被
    系统性拉低好几个点。某天所有列都缺则该天不出现在结果里（下游写 NULL，
    而不是交出一行全 0 的假数据）。聚合口径见
    :data:`newsagg.weather.models.DAILY_AGGREGATION`。
    """
    buckets: dict[date, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    wind: dict[date, list[tuple[float, float]]] = defaultdict(list)
    directions = series_by_col.get("wind_direction_10m") or []
    speeds = series_by_col.get("wind_speed_10m") or []

    for idx, text in enumerate(times):
        day = _to_datetime(text).date()
        for col, series in series_by_col.items():
            value = series[idx] if idx < len(series) else None
            if value is not None:
                buckets[day][col].append(float(value))
        if idx < len(directions) and idx < len(speeds):
            d, s = directions[idx], speeds[idx]
            if d is not None and s is not None:
                wind[day].append((float(d), float(s)))

    out: dict[date, dict[str, float]] = {}
    for day, cols in buckets.items():
        row: dict[str, float] = {}
        for col, how in DAILY_AGGREGATION.items():
            if how == "dominant":
                value = dominant_wind_direction(wind[day])
            else:
                vals = cols.get(col)
                if not vals:
                    continue
                if how == "mean":
                    value = sum(vals) / len(vals)
                elif how == "sum":
                    value = sum(vals)
                elif how == "radiation_sum":
                    value = sum(vals) * RADIATION_HOUR_TO_MJ
                elif how == "max":
                    value = max(vals)
                else:
                    raise ValueError(f"未知的逐日聚合口径 {how!r}（列 {col}）")
            if value is not None:
                row[col] = value
        if row:
            out[day] = row
    return out


def _empty_values() -> dict[str, float | None]:
    return dict.fromkeys(VALUE_COLUMNS, None)


def parse_payload(
    payload: Any,
    stations: Sequence[Station],
    *,
    product: str,
) -> list[WeatherPoint]:
    """把一个响应体解析成数据点。

    Args:
        payload: 接口原始 JSON。多地点时是**数组**，单地点时是对象；
            这里统一按数组处理，单地点也被包成 ``[payload]``。
        stations: **与请求同序**的站点列表。接口对多地点按请求顺序返回
            （返回的 ``latitude``/``longitude`` 是吸附后的格点坐标，无法反查），
            因此只能按下标配对；长度不一致视为协议变更，直接报错而不是猜。
        product: 产品标识，决定 ``source``。

    Returns:
        逐时行 + 逐日行，顺序为「站点 → 逐时(时间序) → 逐日(日期序)」。
    """
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(stations):
        raise ValueError(
            f"响应站点数 {len(items)} 与请求 {len(stations)} 不一致——"
            "接口返回顺序/数量可能已变更，拒绝按下标错配"
        )

    source = PRODUCT_SOURCE.get(product, "open-meteo")
    points: list[WeatherPoint] = []

    for item, station in zip(items, stations, strict=True):
        hourly = item.get("hourly") or {}
        times: list[str] = list(hourly.get("time") or [])

        for idx, iso in enumerate(times):
            values = _empty_values()
            for col in VALUE_COLUMNS:
                series = hourly.get(col)
                if series is not None and idx < len(series):
                    raw = series[idx]
                    values[col] = None if raw is None else float(raw)
            points.append(WeatherPoint(
                station_id=station.station_id,
                adcode=station.adcode,
                province_code=station.province_code,
                product=product,
                granularity="hourly",
                ts=_to_datetime(iso),
                values=values,
                source=source,
            ))

        series_by_col = {col: (hourly.get(col) or []) for col in VALUE_COLUMNS}
        for day, daily_values in sorted(aggregate_daily(times, series_by_col).items()):
            values = _empty_values()
            values.update(daily_values)
            points.append(WeatherPoint(
                station_id=station.station_id,
                adcode=station.adcode,
                province_code=station.province_code,
                product=product,
                granularity="daily",
                ts=datetime(day.year, day.month, day.day),
                values=values,
                source=source,
            ))

    return points


def _safe_json(resp: Any) -> Any:
    """解析响应体；解析失败返回 ``None``。

    错误响应偶尔不是合法 JSON（网关返回的 HTML 502 页等）。这里不能让解析异常
    盖掉真正的 HTTP 错误——拿不到 ``reason`` 只意味着「按未知原因退避重试」。
    """
    try:
        return resp.json()
    except ValueError:
        return None


# --------------------------------------------------------------------------- 客户端


class WeatherClient:
    """Open-Meteo 取数客户端。

    Args:
        batch: 单请求站点数上限。
        period_days: 单请求时间跨度上限（默认 14，卡在计费 1.0 倍的边界）。
        limiter: 限速器；省略时按免费档额度构造。
        max_attempts: 单请求最大尝试次数（不含「等配额窗口翻页」的那几次）。
        max_wait: 单次请求允许的累计等待秒数，超过就中止本片。
        client: 注入的 ``httpx.Client``（测试用 ``MockTransport``）。
    """

    def __init__(
        self,
        *,
        batch: int = DEFAULT_BATCH,
        period_days: int = 14,
        limiter: RateLimiter | None = None,
        max_attempts: int = 4,
        max_wait: float = DEFAULT_MAX_WAIT,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        if batch <= 0:
            raise ValueError("batch 必须为正")
        self.batch = batch
        self.period_days = period_days
        self.limiter = limiter or RateLimiter(min_gap=0.2)
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        self._owns_client = client is None

    # -- 上下文管理 ------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> WeatherClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 单请求 ----------------------------------------------------------
    def _request(self, url: str, params: dict[str, str], weight: float = 1.0) -> Any:
        """发一次请求，带退避重试。``weight`` 是本次请求折算的调用次数。

        429 单独处理：对方会在响应体的 ``reason`` 里写明是哪个配额窗口用满
        （实测 ``"Hourly API request limit exceeded..."``）。认出来就让限速器
        把该窗口记满、睡到翻页后自动继续——**不消耗重试次数**，因为这不是
        「这次请求失败」，而是「现在整点还没到」。认不出来才退回普通退避。
        """
        last: Exception | None = None
        transient = 0
        deadline = time.monotonic() + self.max_wait
        while True:
            self.limiter.acquire(weight)
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last = exc
                transient += 1
                if transient >= self.max_attempts:
                    break
                time.sleep(min(30.0, 2.0 ** transient))
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                reason = error_reason(_safe_json(resp))
                last = RuntimeError(f"HTTP 429 {reason}".rstrip())
                kind = classify_rate_limit(reason)
                if kind is not None and kind != "month":
                    wait = self.limiter.block_until(kind)
                    if time.monotonic() + wait > deadline:
                        raise RuntimeError(
                            f"HTTP 429（{reason}）还需等待 {wait:.0f}s，"
                            f"已超出单次请求累计等待上限 {self.max_wait:.0f}s"
                        )
                    log.warning(
                        "配额用满：%s —— 等待 %.0fs 到窗口翻页后自动继续", reason, wait,
                    )
                    continue
                if kind == "month":
                    # 月额度用满：本轮再等也没有意义，直接中止，别把进程挂住
                    raise RuntimeError(f"HTTP 429（{reason}）：月额度用满，本轮中止")
                transient += 1
                if transient >= self.max_attempts:
                    break
                time.sleep(min(30.0, 2.0 ** transient))
                continue

            if resp.status_code in (500, 502, 503, 504):
                last = RuntimeError(f"HTTP {resp.status_code}")
                transient += 1
                if transient >= self.max_attempts:
                    break
                time.sleep(min(30.0, 2.0 ** transient))
                continue

            # 4xx（参数错、越界）重试没有意义，立刻暴露
            raise RuntimeError(f"HTTP {resp.status_code} {url}：{resp.text[:300]}")

        raise RuntimeError(f"连续 {self.max_attempts} 次失败：{last}")

    # -- 批量取数 --------------------------------------------------------
    def fetch(
        self,
        *,
        product: str,
        stations: Sequence[Station],
        start: date,
        end: date,
        today: date | None = None,
        stats: FetchStats | None = None,
    ) -> list[WeatherPoint]:
        """取数：按 ``batch`` 切站点、按 ``period_days`` 切时间，笛卡尔组合。"""
        if product not in ENDPOINTS:
            raise ValueError(f"未知产品 {product!r}")
        stats = stats if stats is not None else FetchStats()
        url, _ = ENDPOINTS[product]
        out: list[WeatherPoint] = []

        periods = chunk_periods(start, end, days=self.period_days)
        batches = [
            list(stations[i:i + self.batch])
            for i in range(0, len(stations), self.batch)
        ]
        log.info(
            "取数计划：产品=%s 站点=%d（%d 批） 时间=%s..%s（%d 段） 共 %d 请求",
            product, len(stations), len(batches), start, end, len(periods),
            len(batches) * len(periods),
        )

        for p_start, p_end in periods:
            for batch in batches:
                params = build_params(
                    product=product, stations=batch,
                    start=p_start, end=p_end, today=today,
                )
                weight = estimate_call_cost(
                    locations=len(batch),
                    days=(p_end - p_start).days + 1,
                    variables=len(HOURLY_VARS),
                )
                stats.requests += 1
                stats.cost += weight
                before = self.limiter.waits
                payload = self._request(url, params, weight=weight)
                stats.waited += self.limiter.waits - before

                points = parse_payload(payload, batch, product=product)
                for p in points:
                    if p.granularity == "hourly":
                        stats.points_hourly += 1
                    else:
                        stats.points_daily += 1
                    table = p.table
                    stats.per_table[table] = stats.per_table.get(table, 0) + 1
                out.extend(points)
        return out


def resolve_window(
    product: str,
    *,
    start: date,
    end: date,
    today: date | None = None,
) -> tuple[date, date]:
    """按产品把请求窗口夹到**确实有数据**的范围内。

    - ``hist_rt``：ERA5 滞后 5 天 → 结束日截到 ``今天 − 5``。
    - ``hist_fc``：归档从 2022-11-24 开始 → 开始日不早于该日。
    - ``daily_fc``：滚动预报 → 不早于今天（历史部分已由 hist_fc 覆盖）。

    夹完若开始日 > 结束日，返回空窗口的等价形式（调用方据此跳过并记一条
    「无待采窗口」而不是发一个必然 400 的请求）。
    """
    today = today or date.today()
    if product == "hist_rt":
        from newsagg.weather.models import ARCHIVE_LAG_DAYS

        end = min(end, today - timedelta(days=ARCHIVE_LAG_DAYS))
    elif product == "hist_fc":
        floor = date(2022, 11, 24)
        start = max(start, floor)
        end = min(end, today)
    elif product == "daily_fc":
        start = max(start, today)
        end = min(end, today + timedelta(days=15))
    else:
        raise ValueError(f"未知产品 {product!r}")
    return start, end
