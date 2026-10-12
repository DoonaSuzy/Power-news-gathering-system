"""采集层配置（环境变量 + ``.env``）。

所有环境变量统一前缀 ``POWERNEWS_``，通过 ``python-dotenv`` 从项目根的
``.env`` 读取（已存在的真实环境变量优先，便于容器注入）。

数据中台的配置独立在 ``newsplatform/core/config.py``（Pydantic BaseSettings），
两者互不依赖，避免采集层为了读一个端口而引入 Web 框架依赖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from newsagg.errors import ConfigError

__all__ = ["Settings", "get_settings", "reset_settings", "PROJECT_ROOT", "ENV_PREFIX"]

ENV_PREFIX = "POWERNEWS_"

#: 项目根目录（本文件位于 <root>/newsagg/config.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """加载项目根 ``.env``（存在才加载，不覆盖已有环境变量）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        return
    load_dotenv(env_file, override=False)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"{ENV_PREFIX}{key}", default)


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw != "" else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    try:
        return float(raw) if raw != "" else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    """采集层运行时配置快照。"""

    # ---- MySQL（采集库）----
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "powernews"
    mysql_password: str = ""
    mysql_database: str = "powernews"
    mysql_charset: str = "utf8mb4"

    # ---- Redis（队列 + 布隆）----
    redis_url: str = "redis://127.0.0.1:6379/0"
    queue_stream: str = "powernews:site_jobs"
    queue_group: str = "powernews-workers"

    # ---- 采集行为 ----
    concurrency: int = 8
    download_timeout: int = 30
    autothrottle_max_delay: int = 60
    lease_seconds: int = 900
    worker_count: int = 4
    user_agent: str = "powernews/0.1 (educational local aggregator)"

    # ---- 定时调度（cron + 重试）----
    #: cron 表达式（5 字段：分 时 日 月 星期）。空 = 只用「间隔小时」模式。
    #: 例："0 8 * * *" 每天 08:00；"0 */2 * * *" 每两小时。
    #: 两种模式**同时生效**：cron 命中 或 间隔到期，任一满足即建批。
    schedule_cron: str = ""
    #: 单个 job 最大重试次数（超过则标记 failed，不再重认领）。
    job_max_retries: int = 3
    #: 重试的最小空闲间隔（毫秒）。消息空闲超过该值才被重认领 ——
    #: 立即重试会对着同一个失败原因连打，既浪费又刷日志。
    job_retry_min_idle_ms: int = 600_000

    # ---- 窗口 ----
    window_days: int = 7
    incremental_lookback_hours: int = 48

    # ---- 正文抽取 / 去重 ----
    gne_fallback: bool = True
    simhash_threshold: int = 3

    # ---- 日志 ----
    log_dir: str = ""
    log_level: str = "INFO"
    log_json: bool = False

    # ---- 派生 ----
    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)

    # ------------------------------------------------------------------
    @property
    def dsn(self) -> dict[str, object]:
        """PyMySQL 连接参数。"""
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "database": self.mysql_database,
            "charset": self.mysql_charset,
            "autocommit": False,
        }

    @property
    def sql_dir(self) -> Path:
        return self.project_root / "sql"

    @property
    def samples_dir(self) -> Path:
        return self.project_root / "samples"

    def require_password(self) -> str:
        """取数据库密码；为空时抛配置异常（避免静默连匿名库）。"""
        if not self.mysql_password:
            raise ConfigError(
                "未配置 POWERNEWS_MYSQL_PASSWORD。请复制 .env.example 为 .env 并填写。"
            )
        return self.mysql_password

    def describe(self) -> dict[str, object]:
        """脱敏后的配置摘要（用于 ``powernews check``）。"""
        return {
            "mysql": f"{self.mysql_user}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}",
            "mysql_password": "***" if self.mysql_password else "(未设置)",
            "redis": self.redis_url,
            "queue": f"{self.queue_stream}/{self.queue_group}",
            "concurrency": self.concurrency,
            "worker_count": self.worker_count,
            "window_days": self.window_days,
            "user_agent": self.user_agent,
            "gne_fallback": self.gne_fallback,
            "project_root": str(self.project_root),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取全局配置单例（首次调用加载 ``.env``）。"""
    _load_dotenv()
    return Settings(
        mysql_host=_env("MYSQL_HOST", "127.0.0.1"),
        mysql_port=_env_int("MYSQL_PORT", 3306),
        mysql_user=_env("MYSQL_USER", "powernews"),
        mysql_password=_env("MYSQL_PASSWORD", ""),
        mysql_database=_env("MYSQL_DATABASE", "powernews"),
        mysql_charset=_env("MYSQL_CHARSET", "utf8mb4"),
        redis_url=_env("REDIS_URL", "redis://127.0.0.1:6379/0"),
        queue_stream=_env("QUEUE_STREAM", "powernews:site_jobs"),
        queue_group=_env("QUEUE_GROUP", "powernews-workers"),
        concurrency=_env_int("CONCURRENCY", 8),
        download_timeout=_env_int("DOWNLOAD_TIMEOUT", 30),
        autothrottle_max_delay=_env_int("AUTOTHROTTLE_MAX_DELAY", 60),
        lease_seconds=_env_int("LEASE_SECONDS", 900),
        worker_count=_env_int("WORKER_COUNT", 4),
        user_agent=_env("USER_AGENT", "powernews/0.1 (educational local aggregator)"),
        schedule_cron=_env("SCHEDULE_CRON", ""),
        job_max_retries=_env_int("JOB_MAX_RETRIES", 3),
        job_retry_min_idle_ms=_env_int("JOB_RETRY_MIN_IDLE_MS", 600_000),
        window_days=_env_int("WINDOW_DAYS", 7),
        incremental_lookback_hours=_env_int("INCREMENTAL_LOOKBACK_HOURS", 48),
        gne_fallback=_env_bool("GNE_FALLBACK", True),
        simhash_threshold=_env_int("SIMHASH_THRESHOLD", 3),
        log_dir=_env("LOG_DIR", ""),
        log_level=_env("LOG_LEVEL", "INFO"),
        log_json=_env_bool("LOG_JSON", False),
    )


def reset_settings() -> None:
    """清除配置缓存（测试用）。"""
    get_settings.cache_clear()
