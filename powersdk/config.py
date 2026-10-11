"""多环境配置体系（dev / test / prod）。

设计要点
--------
* 环境由环境变量 ``POWERSDK_ENV`` 选择，取值限定 ``dev`` / ``test`` / ``prod``；
  未设置时默认 ``dev``；非法取值回退 ``dev`` 并记录 loguru 警告（库不应因一个
  拼写错误的环境变量而崩溃）。
* 每个环境对应一份 :class:`EnvProfile`：MySQL 库名、日志级别、批处理大小、
  准确率阈值、缓存 TTL 均按环境区分（prod 更严格、批更大）。
* 连接参数可被 ``POWERSDK_MYSQL_*`` / ``POWERSDK_LOG_LEVEL`` 等变量逐项覆盖，
  便于 CI 与本地开发复用同一份代码。
* :func:`reset_settings` 供测试在切换环境变量后清空单例缓存。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote_plus

from loguru import logger

__all__ = [
    "ENVIRONMENTS",
    "DEFAULT_ENV",
    "ENV_VAR",
    "ENV_PROFILES",
    "EnvProfile",
    "Settings",
    "get_settings",
    "reset_settings",
    "current_env",
    "settings_for_env",
    "normalize_env",
]

#: 支持的环境名（顺序即优先级展示顺序）
ENVIRONMENTS: tuple[str, ...] = ("dev", "test", "prod")

#: 未设置 ``POWERSDK_ENV`` 时的默认环境
DEFAULT_ENV: str = "dev"

#: 环境选择变量名
ENV_VAR: str = "POWERSDK_ENV"

#: 统一时区（东八区，无夏令时，naive 时间戳即本地墙钟）
DEFAULT_TZ: str = "Asia/Shanghai"


@dataclass(frozen=True)
class EnvProfile:
    """单个运行环境的默认画像。"""

    env: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    log_level: str
    batch_size: int
    accuracy_threshold: float
    cache_ttl: int
    description: str = ""


#: dev / test / prod 三套环境画像
ENV_PROFILES: dict[str, EnvProfile] = {
    "dev": EnvProfile(
        env="dev",
        mysql_host="127.0.0.1",
        mysql_port=3306,
        mysql_user="powersdk",
        mysql_password="devpass",
        mysql_database="power_data_dev",
        log_level="DEBUG",
        batch_size=5_000,
        accuracy_threshold=0.99,
        cache_ttl=60,
        description="本地开发：详细日志、小批量、宽松准确率阈值",
    ),
    "test": EnvProfile(
        env="test",
        mysql_host="127.0.0.1",
        mysql_port=3306,
        mysql_user="powersdk",
        mysql_password="testpass",
        mysql_database="power_data_test",
        log_level="INFO",
        batch_size=10_000,
        accuracy_threshold=0.998,
        cache_ttl=30,
        description="测试/CI：内存或影子库、标准阈值",
    ),
    "prod": EnvProfile(
        env="prod",
        mysql_host="mysql-master.internal",
        mysql_port=3306,
        mysql_user="powersdk_ro",
        mysql_password="",
        mysql_database="power_data_prod",
        log_level="WARNING",
        batch_size=100_000,
        accuracy_threshold=0.999,
        cache_ttl=300,
        description="生产：只读账号、大批量、严格阈值",
    ),
}


@dataclass(frozen=True)
class Settings:
    """SDK 运行时配置（不可变，派生自环境变量 + 环境画像）。"""

    env: str = DEFAULT_ENV
    tz: str = DEFAULT_TZ

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "powersdk"
    mysql_password: str = ""
    mysql_database: str = "power_data_dev"

    log_level: str = "DEBUG"
    batch_size: int = 5_000
    accuracy_threshold: float = 0.998
    cache_ttl: int = 60

    # ------------------------------------------------------------------ 派生
    @property
    def profile(self) -> EnvProfile:
        """当前环境画像。"""
        return ENV_PROFILES.get(self.env, ENV_PROFILES[DEFAULT_ENV])

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def dsn(self) -> str:
        """MySQL DSN（SQLAlchemy / PyMySQL 风格）。"""
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password) if self.mysql_password else ""
        auth = f"{user}:{password}" if password else user
        return f"mysql+pymysql://{auth}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"

    # ------------------------------------------------------------------ 构造
    @classmethod
    def for_env(cls, env: str | None = None, **overrides: Any) -> Settings:
        """按环境名构造配置（不读取环境变量，便于测试）。"""
        name = normalize_env(env)
        profile = ENV_PROFILES[name]
        settings = cls(
            env=name,
            tz=overrides.pop("tz", DEFAULT_TZ),
            mysql_host=profile.mysql_host,
            mysql_port=profile.mysql_port,
            mysql_user=profile.mysql_user,
            mysql_password=profile.mysql_password,
            mysql_database=profile.mysql_database,
            log_level=profile.log_level,
            batch_size=profile.batch_size,
            accuracy_threshold=profile.accuracy_threshold,
            cache_ttl=profile.cache_ttl,
        )
        if overrides:
            settings = replace(settings, **overrides)
        return settings

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """从环境变量派生配置。

        读取 ``POWERSDK_ENV`` 选择环境，再用 ``POWERSDK_MYSQL_*`` 等变量覆盖。
        """
        env_map: Mapping[str, str] = os.environ if environ is None else environ
        name = normalize_env(env_map.get(ENV_VAR))
        settings = cls.for_env(name)

        def _str(key: str, default: str) -> str:
            raw = env_map.get(key)
            return default if raw in (None, "") else str(raw)

        def _int(key: str, default: int) -> int:
            raw = env_map.get(key)
            if raw in (None, ""):
                return default
            try:
                return int(raw)
            except ValueError:
                logger.warning("环境变量 {} 不是整数：{!r}，使用默认值 {}", key, raw, default)
                return default

        def _float(key: str, default: float) -> float:
            raw = env_map.get(key)
            if raw in (None, ""):
                return default
            try:
                return float(raw)
            except ValueError:
                logger.warning("环境变量 {} 不是数字：{!r}，使用默认值 {}", key, raw, default)
                return default

        return replace(
            settings,
            tz=_str("POWERSDK_TZ", settings.tz),
            mysql_host=_str("POWERSDK_MYSQL_HOST", settings.mysql_host),
            mysql_port=_int("POWERSDK_MYSQL_PORT", settings.mysql_port),
            mysql_user=_str("POWERSDK_MYSQL_USER", settings.mysql_user),
            mysql_password=_str("POWERSDK_MYSQL_PASSWORD", settings.mysql_password),
            mysql_database=_str("POWERSDK_MYSQL_DATABASE", settings.mysql_database),
            log_level=_str("POWERSDK_LOG_LEVEL", settings.log_level).upper(),
            batch_size=_int("POWERSDK_BATCH_SIZE", settings.batch_size),
            accuracy_threshold=_float("POWERSDK_ACCURACY_THRESHOLD", settings.accuracy_threshold),
            cache_ttl=_int("POWERSDK_CACHE_TTL", settings.cache_ttl),
        )

    def as_dict(self) -> dict[str, Any]:
        """导出为可日志化/可序列化的字典（密码脱敏）。"""
        return {
            "env": self.env,
            "tz": self.tz,
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_user": self.mysql_user,
            "mysql_database": self.mysql_database,
            "log_level": self.log_level,
            "batch_size": self.batch_size,
            "accuracy_threshold": self.accuracy_threshold,
            "cache_ttl": self.cache_ttl,
        }


def normalize_env(env: str | None) -> str:
    """归一化环境名；未知/空值回退 :data:`DEFAULT_ENV`。"""
    if env is None or str(env).strip() == "":
        return DEFAULT_ENV
    name = str(env).strip().lower()
    if name in ENV_PROFILES:
        return name
    logger.warning("未知环境 {!r}（支持 {}），回退为 {!r}", env, "/".join(ENVIRONMENTS), DEFAULT_ENV)
    return DEFAULT_ENV


# 全局单例（惰性）
_settings: Settings | None = None


def get_settings() -> Settings:
    """获取进程级配置单例（首次调用时从环境变量派生）。"""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def settings_for_env(env: str | None = None) -> Settings:
    """便捷函数：按环境名构造配置并缓存为单例。"""
    global _settings
    _settings = Settings.for_env(env)
    return _settings


def current_env() -> str:
    """当前生效的环境名。"""
    return get_settings().env


def reset_settings() -> None:
    """清空配置单例缓存（测试切环境后必须调用）。"""
    global _settings
    _settings = None
