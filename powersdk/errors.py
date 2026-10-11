"""powersdk 异常体系。

所有 SDK 内部异常均继承 :class:`PowerSDKError`，业务侧可统一捕获：:

    try:
        df = client.query("load", region="广东")
    except PowerSDKError as exc:
        ...
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "PowerSDKError",
    "ConfigError",
    "DataSourceError",
    "UnknownCategoryError",
    "UnsupportedCategoryError",
    "UnavailableParamError",
    "UnknownRegionError",
    "UnknownParamError",
    "ValidationError",
]


class PowerSDKError(Exception):
    """SDK 统一异常基类。"""


class ConfigError(PowerSDKError):
    """配置非法（未知环境、缺失必填参数等）。"""


class DataSourceError(PowerSDKError):
    """数据源不可用（未安装 PyMySQL、无法连接 MySQL 等）。"""


class UnknownCategoryError(PowerSDKError):
    """未知的时序数据类别。"""

    def __init__(self, name: object, available: Iterable[str] = ()) -> None:
        self.name = name
        self.available: tuple[str, ...] = tuple(available)
        hint = f"，可用类别：{', '.join(self.available)}" if self.available else ""
        super().__init__(f"未知的时序数据类别：{name!r}{hint}")


class UnsupportedCategoryError(PowerSDKError):
    """类别是合法的，但**这个数据源里没有它的数据**。

    与 :class:`UnknownCategoryError` 区分开很重要：前者是「你写错了」，
    后者是「我们还没采」。后者必须响亮地失败——返回一份合成数据或空表，
    调用方会以为自己拿到了真的负荷曲线。
    """

    def __init__(
        self, category: object, *, source: str, available: Iterable[str] = ()
    ) -> None:
        self.category = category
        self.source = source
        self.available: tuple[str, ...] = tuple(available)
        hint = f"；{source} 侧已接通：{', '.join(self.available)}" if self.available else ""
        super().__init__(
            f"数据源 {source!r} 没有 {category!r} 的数据表{hint}。"
            f"离线演示请改用 InMemoryTimeSeriesSource（合成数据，仅用于跑通流程）"
        )


class UnavailableParamError(PowerSDKError):
    """参数名 SDK 认识，但当前库里没有采集这一列。"""

    def __init__(self, param: object, available: Iterable[str] = ()) -> None:
        self.param = param
        self.available: tuple[str, ...] = tuple(available)
        hint = f"，库里已采集：{', '.join(self.available)}" if self.available else ""
        super().__init__(f"参数 {param!r} 在气象库未采集{hint}")


class UnknownRegionError(PowerSDKError):
    """无法解析的地区名（既不是省名/拼音/简称/行政区划码）。"""

    def __init__(self, name: object, available: Iterable[str] = ()) -> None:
        self.name = name
        self.available: tuple[str, ...] = tuple(available)
        hint = f"，支持 {len(self.available)} 个省级行政区" if self.available else ""
        super().__init__(f"无法解析地区：{name!r}{hint}")


class UnknownParamError(PowerSDKError):
    """未知的气象参数名。"""

    def __init__(self, name: object, available: Iterable[str] = ()) -> None:
        self.name = name
        self.available: tuple[str, ...] = tuple(available)
        hint = f"，可用参数：{', '.join(self.available)}" if self.available else ""
        super().__init__(f"未知的气象参数：{name!r}{hint}")


class ValidationError(PowerSDKError):
    """数据校验未达标（准确率低于阈值且配置为严格模式）。"""

    def __init__(self, message: str, *, accuracy: float = 0.0, threshold: float = 0.998) -> None:
        self.accuracy = accuracy
        self.threshold = threshold
        super().__init__(f"{message}（准确率 {accuracy:.4f} < 阈值 {threshold:.4f}）")
