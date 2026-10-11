"""PowerDataClient 门面：业务人员 3 行代码拿到标准化时序数据。

典型用法（简历口径的「3 行代码」）：::

    from powersdk import PowerDataClient

    client = PowerDataClient()
    df = client.query("load", region="广东", start="2025-01-01",
                      end="2025-01-02", granularity="hourly")

接真实数据仍然是 3 行，只是先把中台地址配进环境（代码一行都不用改）：::

    export POWERSDK_API_BASE_URL=http://192.168.101.84:8080/api/v1
    export POWERSDK_API_TOKEN=pk_xxxxxxxx

    df = PowerDataClient().query("weather", region="广东", start="2024-01-10",
                                 end="2024-01-11")

``query`` 内部管线（全部向量化、各环节可注入替换）：

1. 类别解析（中文别名 / 枚举 / 规范名）；
2. 地区解析（pypinyin 拼音 / 缩写 / 行政区划码）；
3. 通过 :class:`~powersdk.timeseries.TimeSeriesSource` 取数（中台 HTTP / MySQL /
   内存，见 :func:`default_source`）；
4. JSON 解析 + 时间戳补全 + 排序去重；
5. 稳健离群清洗（可选）、时间轴补缺（按插值方法表）；
6. 多粒度重采样 + 单位归一；
7. 数据校验 + 告警（loguru / 自定义 AlertSink）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import Any

import pandas as pd
from loguru import logger

from .config import Settings, get_settings
from .errors import PowerSDKError
from .http_source import HttpTimeSeriesSource
from .interpolation import resolve_method
from .parsing import parse_json_records, standardize_records
from .timeseries import (
    CANONICAL_COLUMNS,
    Category,
    InMemoryTimeSeriesSource,
    MySQLTimeSeriesSource,
    TimeSeriesSource,
    backed_categories,
    categories,
    category_spec,
    granularity_freq,
    resolve_category,
    resolve_granularity,
)
from .utils import clean_outliers, fill_gaps, resample, standardize_units
from .validation import (
    AlertSink,
    CollectingAlertSink,
    ValidationReport,
    spec_for_category,
    validate_and_alert,
)

__all__ = ["PowerDataClient"]


def default_source(settings: Settings | None = None) -> TimeSeriesSource:
    """按「这台机器实际配了什么」挑数据源：中台 HTTP > MySQL 直连 > 内存演示。

    判据只看**环境变量**，不看 :class:`Settings` 字段——dev/test/prod 三套环境画像
    里 ``api_base_url`` 永远非空（本地 127.0.0.1、生产 power-data.internal），
    拿它当判据会让每一次 ``PowerDataClient()`` 都去打一个不存在的端口，
    离线用例与「开箱即演示」两头都毁掉。业务同学配好中台地址（或库口令）
    就等于表态「我要真数据」，一行构造代码都不用改。
    """
    environ = os.environ
    if environ.get("POWERSDK_API_BASE_URL"):
        return HttpTimeSeriesSource(settings=settings or get_settings())
    if environ.get("POWERSDK_MYSQL_PASSWORD"):
        return MySQLTimeSeriesSource(settings=settings or get_settings())
    return InMemoryTimeSeriesSource()


class PowerDataClient:
    """电力数据中台统一接口客户端。

    :param source: 数据源；不传则按环境变量自动选（见 :func:`default_source`），
        既没配中台也没配库时回落到内存演示源
    :param env: 环境名（dev/test/prod），优先级高于 ``POWERSDK_ENV``
    :param alert_sink: 告警出口，默认收集到内存（``client.alerts`` 可读）
    :param fill: 是否补齐时间轴缺口（默认 True，按插值方法表）
    :param clean: 是否做稳健离群清洗（默认 False，避免改变原始口径）
    :param validate_data: 是否执行校验与告警（默认 True）
    """

    def __init__(
        self,
        source: TimeSeriesSource | None = None,
        *,
        settings: Settings | None = None,
        env: str | None = None,
        alert_sink: AlertSink | None = None,
        fill: bool = True,
        clean: bool = False,
        validate_data: bool = True,
        logger_: Any | None = None,
    ) -> None:
        self.settings = settings or (Settings.for_env(env) if env else get_settings())
        self.source: TimeSeriesSource = source or default_source(self.settings)
        self.alert_sink: AlertSink = alert_sink or CollectingAlertSink()
        self.fill = fill
        self.clean = clean
        self.validate_data = validate_data
        self.log = logger_ or logger.bind(component="PowerDataClient", env=self.settings.env)
        self.last_report: ValidationReport | None = None

    # ------------------------------------------------------------------ 查询
    def query(
        self,
        category: Category | str,
        region: Sequence[str] | str | None = None,
        start: object = None,
        end: object = None,
        granularity: str = "hourly",
        *,
        param: str | None = None,
        fill: bool | None = None,
        clean: bool | None = None,
        method: str | None = None,
        unit: str | None = None,
        validate: bool | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """查询标准化时序数据，返回 ``[ts, region, value, unit]`` DataFrame。"""
        started = time.perf_counter()
        cat = resolve_category(category)
        spec = category_spec(cat)
        gran = resolve_granularity(granularity)
        use_fill = self.fill if fill is None else fill
        use_clean = self.clean if clean is None else clean
        use_validate = self.validate_data if validate is None else validate
        interp = resolve_method(param or spec.interp_param, method)

        self.log.debug(
            "开始查询 category={} region={} granularity={} param={} interp={}",
            cat.value, region, gran, param, interp,
        )

        raw = self.source.fetch(cat, region=region, start=start, end=end, granularity="raw", param=param)
        if raw is None or len(raw) == 0:
            self.log.warning("查询结果为空 category={} region={} start={} end={}", cat.value, region, start, end)
            return pd.DataFrame({column: pd.Series(dtype="object") for column in CANONICAL_COLUMNS})

        df = standardize_records(raw, unit=unit or spec.unit, tz=self.settings.tz)
        if use_clean:
            df = clean_outliers(df)
        if use_fill:
            df = fill_gaps(df, method=interp, freq="h", param=param)
        df = resample(df, gran)
        df = standardize_units(df, target=unit or spec.unit)
        if limit is not None:
            df = df.head(int(limit)).reset_index(drop=True)

        if use_validate:
            # 校验区间按「逐时口径」派生：这里取的是逐时行、本地重采样成均值，输出
            # 仍是小时量纲（库里存的逐日行是日累计，量纲不同，见 weather_range）。
            # expected_freq 则跟着输出粒度走，否则拿 1 小时网格去卡日行，缺口率会
            # 虚报成接近 100%，刷出一屏没有信息量的告警。
            v_spec = spec_for_category(cat, param, expected_freq=granularity_freq(gran))
            self.last_report = validate_and_alert(
                df, v_spec, self.alert_sink, threshold=self.settings.accuracy_threshold
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        self.log.info(
            "查询完成 category={} region={} granularity={} rows={} regions={} accuracy={} elapsed_ms={:.1f}",
            cat.value, region, gran, len(df),
            int(df["region"].nunique()) if len(df) else 0,
            f"{self.last_report.accuracy:.4f}" if self.last_report else "n/a",
            elapsed_ms,
        )
        return df

    def query_json(self, payload: Any, *, category: str, granularity: str = "hourly") -> pd.DataFrame:
        """直接消费上游 JSON 载荷（解析 + 补全 + 重采样 + 校验）。"""
        frame = parse_json_records(payload, category=category, tz=self.settings.tz)
        if len(frame) == 0:
            self.last_report = None
            return frame
        df = standardize_records(frame, tz=self.settings.tz)
        df = resample(df, resolve_granularity(granularity))
        v_spec = spec_for_category(category)
        self.last_report = validate_and_alert(
            df, v_spec, self.alert_sink, threshold=self.settings.accuracy_threshold
        )
        return df

    # ------------------------------------------------------------------ 元信息
    def categories(self) -> tuple[str, ...]:
        """可查询的 12 类时序数据。"""
        return categories()

    def regions(self) -> tuple[str, ...]:
        """数据源覆盖的地区。"""
        getter = getattr(self.source, "available_regions", None)
        return tuple(getter()) if callable(getter) else ()

    @property
    def alerts(self) -> tuple[Any, ...]:
        """已收集的告警（默认 sink 会累积在内存中）。"""
        return tuple(getattr(self.alert_sink, "alerts", ()))

    def last_accuracy(self) -> float | None:
        """最近一次查询的准确率。"""
        return self.last_report.accuracy if self.last_report else None

    def healthcheck(self) -> dict[str, Any]:
        """自检信息（配置脱敏 + 数据源可用性）。

        探针固定用 ``weather`` + ``2025-01-01``：这是三类数据源（内存 / MySQL /
        HTTP）都有数的一个日期；换成 ``load`` 会让自检永远是 degraded，
        因为真实库里目前根本没有负荷采集器。
        """
        info: dict[str, Any] = {"env": self.settings.env, "settings": self.settings.as_dict()}
        # settings 里的 api_token 来自环境变量；HTTP 源也可以自己带 token。
        # 只看前者会让自检说「没配 Token」而实际请求正在发 Bearer——排查鉴权时
        # 这是最误导人的一种报告。这里只纠正「有没有」，值仍然脱敏。
        if getattr(self.source, "token", ""):
            info["settings"]["api_token"] = "***"
        info["source"] = type(self.source).__name__
        info["backed_categories"] = backed_categories()
        try:
            probe = self.source.fetch("weather", start="2025-01-01", end="2025-01-01")
            info["source_rows"] = int(len(probe))
            info["status"] = "ok"
        except PowerSDKError as exc:  # 例如 MySQL 源离线
            info["status"] = "degraded"
            info["detail"] = str(exc)
        return info

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PowerDataClient(env={self.settings.env!r}, source={type(self.source).__name__})"
