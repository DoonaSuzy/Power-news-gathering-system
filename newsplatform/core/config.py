"""中台配置（Pydantic BaseSettings，改造方案 §2.4 步骤 1）。

对应旧项目的 Flask ``Config`` 类层次：Flask 用类继承 + ``app.config.from_object``，
这里换成 Pydantic BaseSettings——类型即文档，缺失/类型错误在启动时立刻暴露，
而不是等到某个请求打进来才 500。

环境变量前缀统一 ``POWERNEWS_PLATFORM_``，与采集层的 ``POWERNEWS_`` 不冲突
（两者可共存于同一个 ``.env``）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["PlatformSettings", "get_platform_settings", "reset_platform_settings", "ENV_PREFIX"]

ENV_PREFIX = "POWERNEWS_PLATFORM_"

#: 项目根目录（本文件位于 <root>/newsplatform/core/config.py）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv_into_environ() -> None:
    """把项目根的 ``.env`` 也灌进 ``os.environ``。

    :class:`PlatformSettings` 通过 pydantic-settings 的 ``env_file`` 读 ``.env``，
    但那些值**只进模型、不进 ``os.environ``**。于是任何用 ``os.getenv()`` 读配置的
    代码都拿不到值 —— 而且失败方式是**静默回落默认值**：LLM 客户端读不到 api key
    会安静地判定「未配置」，公告退化成标题罗列，界面看不出哪里错了。

    实测踩过：``/api/v1/notices/status`` 返回 ``llm_configured: false``，
    而 ``.env`` 里明明配好了三个变量。

    ``override=False``：真实环境变量优先于文件，符合十二要素应用惯例。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # 没装 python-dotenv 时保持现状，不阻断启动
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


# 模块导入即加载，保证任何 ``os.getenv`` 都能读到与 PlatformSettings 一致的值
_load_dotenv_into_environ()


class PlatformSettings(BaseSettings):
    """数据中台运行时配置。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )
    # ---------------- 应用 ----------------
    app_name: str = "Power News Platform"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000

    #: 生产环境务必置 false：为 true 时开放 /docs 且不回显内部异常信息
    debug: bool = False
    #: 是否开放 OpenAPI 文档（/docs、/redoc、/openapi.json）
    docs_enabled: bool = True

    # ---------------- 数据库 ----------------
    # Security: 源码中不内置真实口令。默认值里的 CHANGE_ME 是占位符，
    # 请通过环境变量 POWERNEWS_PLATFORM_DATABASE_URL / _COLLECTOR_DATABASE_URL
    # （或 .env，见 .env.example）注入真实连接串。
    #: 中台自有库（可写）：api_keys / audit_logs / stats_snapshots
    database_url: str = (
        "mysql+aiomysql://powernews:CHANGE_ME@127.0.0.1:3306"
        "/powernews_platform?charset=utf8mb4"
    )
    #: 采集库（只读语义）：articles / sources / provinces / crawl_jobs
    collector_database_url: str = (
        "mysql+aiomysql://powernews:CHANGE_ME@127.0.0.1:3306"
        "/powernews?charset=utf8mb4"
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    # ---------------- Redis ----------------
    redis_url: str = "redis://127.0.0.1:6380/1"
    cache_ttl_seconds: int = 60

    # ---------------- 安全 ----------------
    secret_key: str = "change-me-in-production"
    #: 引导令牌：库内尚无任何 API Key 时可用它访问管理接口（便于首次初始化）
    bootstrap_admin_token: str = ""
    access_token_expire_minutes: int = 120
    rate_limit_per_minute: int = 120

    # ---------------- 跨域 ----------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ---------------- 查询约束 ----------------
    default_page_size: int = 20
    max_page_size: int = 100

    # ---------------- 新闻智能总结（LLM）----------------
    #: 总开关。默认关：没配 Key 时不该让接口去撞一次必然失败的请求。
    llm_enabled: bool = False
    #: OpenAI 兼容的 base url。换成任一兼容实现（自建 vLLM / 通义 / DeepSeek 等）
    #: 只需改这一项，代码不感知厂商。
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 45.0
    #: 单次总结最多喂给模型的字符数。超了按文章顺序截断 ——
    #: 全量正文几十万字必然超上下文，且费用与窗口内文章数成正比。
    llm_max_input_chars: int = 24_000
    llm_max_tokens: int = 1_200

    # ------------------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """允许 ``a,b`` 这种逗号分隔写法（除 JSON 数组之外）。"""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("secret_key")
    @classmethod
    def _warn_default_secret(cls, value: str) -> str:
        return value

    # ---------------- 派生属性 ----------------
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def llm_ready(self) -> bool:
        """真正可用的判据：开关打开 **且** 三项必填都不为空。

        只看 ``llm_enabled`` 会让「开了开关但忘填 Key」变成运行时 401，
        而不是一个可以在启动时就报出来的配置错误。
        """
        return bool(
            self.llm_enabled
            and self.llm_api_key.strip()
            and self.llm_base_url.strip()
            and self.llm_model.strip()
        )

    @property
    def docs_url(self) -> str | None:
        return "/docs" if self.docs_enabled else None

    @property
    def redoc_url(self) -> str | None:
        return "/redoc" if self.docs_enabled else None

    @property
    def openapi_url(self) -> str | None:
        return f"{self.api_prefix}/openapi.json" if self.docs_enabled else None

    def describe(self) -> dict[str, object]:
        """脱敏后的配置摘要（健康检查与启动日志用）。"""
        return {
            "app": f"{self.app_name} {self.app_version}",
            "api_prefix": self.api_prefix,
            "debug": self.debug,
            "docs": self.docs_enabled,
            "platform_db": _mask_dsn(self.database_url),
            "collector_db": _mask_dsn(self.collector_database_url),
            "redis": self.redis_url,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "page_size": f"{self.default_page_size}-{self.max_page_size}",
        }


def _mask_dsn(dsn: str) -> str:
    """把 ``user:password@host`` 里的密码替换为 ``***``。"""
    if "@" not in dsn or "//" not in dsn:
        return dsn
    head, tail = dsn.split("//", 1)
    creds, rest = tail.split("@", 1)
    if ":" in creds:
        user, _pw = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{head}//{creds}@{rest}"


@lru_cache(maxsize=1)
def get_platform_settings() -> PlatformSettings:
    """取配置单例。"""
    return PlatformSettings()


def reset_platform_settings() -> None:
    """清除配置缓存（测试用）。"""
    get_platform_settings.cache_clear()
