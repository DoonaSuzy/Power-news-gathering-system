"""LLM 接入层：OpenAI 兼容协议的异步客户端。

设计约束
--------
1. **可插拔**：只依赖 OpenAI 的 ``POST {base_url}/chat/completions`` 这个事实标准
   形状。换厂商（自建 vLLM / DeepSeek / 通义 / 智谱）只改 ``POWERNEWS_LLM_BASE_URL``
   与 ``POWERNEWS_LLM_MODEL``，代码不感知厂商。
2. **不假装成功**：没配置就抛 :class:`LLMNotConfigured`，不返回一个空壳结果让上层
   误以为「AI 总结完了，只是没内容」。上层（``services/summary_service.py``）
   据此**显式降级**到抽取式，并在响应里写明用的是哪条路径。
3. **失败要能被降级**：网络错、超时、401、5xx、返回体不是合法 JSON、返回的
   JSON 缺字段 —— 全部收敛成 :class:`LLMUnavailable`，绝不让异常穿透成 500。

响应契约
--------
要求模型只输出 JSON（``response_format={"type": "json_object"}``，不支持的厂商
会忽略该字段，所以还要有 ``_loads_lenient`` 兜底：从 ```json 围栏或散文里
抠出第一段平衡花括号）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import httpx

from newsagg.logutil import get_logger
from newsplatform.core.config import PlatformSettings, get_platform_settings

__all__ = [
    "LLMNotConfigured",
    "LLMUnavailable",
    "LLMSettings",
    "LLMClient",
    "build_llm_client",
]

log = get_logger("newsplatform.llm")

#: 期望模型返回的字段（缺任一即视为不可用，不要用半个结果去拼响应）
REQUIRED_KEYS: Final[tuple[str, ...]] = ("headline", "key_points")


class LLMNotConfigured(RuntimeError):
    """未配置 LLM（开关关闭或缺 api key / base url / model）。"""


class LLMUnavailable(RuntimeError):
    """配置到位但这次调用没成功（网络、超时、鉴权、上游错误、响应不合法）。"""


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """从 :class:`PlatformSettings` 抽出来的 LLM 配置快照。"""

    enabled: bool
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_input_chars: int
    max_tokens: int

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.api_key.strip()
            and self.base_url.strip()
            and self.model.strip()
        )

    def describe(self) -> dict[str, object]:
        """给 ``/…/summary`` 响应用的配置说明。**不含 api_key**。"""
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "model": self.model,
            "base_url": self.base_url,
        }


def settings_from(settings: PlatformSettings | None = None) -> LLMSettings:
    """把中台配置翻成 LLM 配置。"""
    cfg = settings or get_platform_settings()
    return LLMSettings(
        enabled=bool(cfg.llm_enabled),
        base_url=cfg.llm_base_url.strip(),
        api_key=cfg.llm_api_key.strip(),
        model=cfg.llm_model.strip(),
        timeout=float(cfg.llm_timeout_seconds),
        max_input_chars=int(cfg.llm_max_input_chars),
        max_tokens=int(cfg.llm_max_tokens),
    )


def _loads_lenient(raw: str) -> dict[str, Any]:
    """尽最大努力把模型输出解析成 dict。

    模型即使被要求输出 JSON，也常会包一层 ```json 围栏，或在前后加上
    「好的，以下是总结：」。直接 ``json.loads`` 会失败，于是这里依次尝试：
    整体解析 → 剥围栏 → 抠第一段平衡花括号。

    Raises:
        LLMUnavailable: 三种尝试都失败。
    """
    text = (raw or "").strip()
    if not text:
        raise LLMUnavailable("模型返回空内容")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except json.JSONDecodeError as exc:
                        raise LLMUnavailable(f"模型返回的 JSON 无法解析：{exc}") from exc
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise LLMUnavailable("模型返回内容里找不到 JSON 对象")


class LLMClient:
    """异步 LLM 客户端。

    Args:
        settings: 配置快照。
        client: 注入的 ``httpx.AsyncClient``（单测用 ``MockTransport``）。
            省略时自行创建并在 :meth:`aclose` 中关闭。

    Example:
        >>> client = build_llm_client()
        >>> client.configured
        False
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or settings_from()
        self._client = client
        self._owns_client = client is None

    # ---------------- 生命周期 ----------------
    @property
    def configured(self) -> bool:
        """True 表示可以尝试调用（不代表这次一定成功）。"""
        return self.settings.ready

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------- 调用 ----------------
    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """发一次 chat 请求并返回解析后的 JSON 对象。

        温度默认 0.2 而不是 0：完全贪心解码在长中文提示上容易陷入重复，
        0.2 基本等价于确定性，但不会被「同一句话刷 10 遍」卡住。

        Raises:
            LLMNotConfigured: 未配置。
            LLMUnavailable: 网络/鉴权/上游/解析任一环节失败。
        """
        if not self.configured:
            raise LLMNotConfigured(
                "未配置 LLM：需要 POWERNEWS_PLATFORM_LLM_ENABLED=1，并填好 "
                "POWERNEWS_PLATFORM_LLM_API_KEY / POWERNEWS_PLATFORM_LLM_BASE_URL / "
                "POWERNEWS_PLATFORM_LLM_MODEL"
            )

        url = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": self.settings.max_tokens,
            # 不支持的厂商会忽略它，`_loads_lenient` 负责兜底
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http().post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"调用 LLM 失败（网络）：{exc}") from exc

        if resp.status_code != 200:
            raise LLMUnavailable(
                f"调用 LLM 失败（HTTP {resp.status_code}）：{resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMUnavailable(f"LLM 响应不是合法 JSON：{exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"LLM 响应缺少 choices[0].message.content：{body}") from exc

        parsed = _loads_lenient(str(content))
        missing = [k for k in REQUIRED_KEYS if k not in parsed]
        if missing:
            raise LLMUnavailable(f"LLM 返回的 JSON 缺少必需字段 {missing}")
        return parsed


def build_llm_client(
    settings: PlatformSettings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> LLMClient:
    """按中台配置构造 LLM 客户端。"""
    return LLMClient(settings_from(settings), client=client)
