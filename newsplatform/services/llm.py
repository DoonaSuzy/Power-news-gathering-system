"""服务端 LLM 客户端（OpenAI 兼容协议）。

为什么服务端也要有一份
----------------------
公告栏的数据要**预生成并缓存**：服务器启动时算一次，之后每 2 小时算一次，
所有用户都读缓存。如果总结放在前端做，每个访客一次页面加载就是一次 LLM 调用 ——
token 消耗与访客数成正比，且内容还会因缓存失效而前后不一致。

协议与前端 ``web/src/api/ai-llm.js`` 保持一致（OpenAI 兼容
``POST {base}/chat/completions``），换供应商只改环境变量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from newsagg.logutil import get_logger

log = get_logger("newsplatform.llm")

#: 期望模型返回的字段，**按能力分别声明**。
#: 曾经是一份全局清单：explain 返回 paragraphs/entities/terms、tag 返回 dimensions，
#: 都被判「缺 headline/key_points」而静默降级 —— 前端踩过这个坑，这里不再犯。
REQUIRED_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    "summarize": ("headline", "key_points"),
    "explain": (),
    "tag": (),
}


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """LLM 配置快照。"""

    base_url: str
    api_key: str
    model: str
    timeout: float
    max_tokens: int

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    def describe(self) -> dict[str, Any]:
        """给接口响应用的配置说明。**不含 api_key**。"""
        return {
            "ready": self.ready,
            "model": self.model,
            "base_url": self.base_url,
        }


def settings_from_env(env: dict[str, str] | None = None) -> LlmSettings:
    """读 LLM 配置。

    优先走 :class:`PlatformSettings`（与中台其它配置同一套加载逻辑，
    包括 ``.env`` 文件与 ``POWERNEWS_PLATFORM_`` 前缀）；
    只有测试显式传入 ``env`` 时才直接用那个 dict。
    """
    if env is not None:
        e = env
        return LlmSettings(
            base_url=(e.get("POWERNEWS_PLATFORM_LLM_BASE_URL") or "").strip(),
            api_key=(e.get("POWERNEWS_PLATFORM_LLM_API_KEY") or "").strip(),
            model=(e.get("POWERNEWS_PLATFORM_LLM_MODEL") or "").strip(),
            timeout=float(e.get("POWERNEWS_PLATFORM_LLM_TIMEOUT_SECONDS") or 60),
            max_tokens=int(e.get("POWERNEWS_PLATFORM_LLM_MAX_TOKENS") or 8000),
        )

    from newsplatform.core.config import get_platform_settings

    cfg = get_platform_settings()
    return LlmSettings(
        base_url=str(cfg.llm_base_url).strip(),
        api_key=str(cfg.llm_api_key).strip(),
        model=str(cfg.llm_model).strip(),
        timeout=float(cfg.llm_timeout_seconds),
        max_tokens=int(cfg.llm_max_tokens),
    )


class LlmError(RuntimeError):
    """LLM 调用失败（未配置 / 网络 / 鉴权 / 上游 / 响应不合法）。"""


def _run(coro: Any) -> Any:
    """在同步入口里跑协程。没有运行中的循环时新建一个并关闭。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已有循环（极少数在 async 上下文里调同步入口的场景）：交给线程池
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def parse_lenient(raw: str) -> dict[str, Any]:
    """尽最大努力把模型输出解析成 dict。

    依次尝试：整体解析 → 剥 Markdown 围栏 → 抠第一段平衡花括号。
    不这么做的话，模型偶尔包一层 ```json 就会让整次预生成失败。
    """
    text = (raw or "").strip()
    if not text:
        raise LlmError("模型返回空内容")

    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass

    if "```" in text:
        for block in text.split("```"):
            b = block.strip()
            if b.lower().startswith("json"):
                b = b[4:].strip()
            try:
                v = json.loads(b)
                if isinstance(v, dict):
                    return v
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
                        v = json.loads(text[start:i + 1])
                    except json.JSONDecodeError as exc:
                        raise LlmError(f"模型返回的 JSON 无法解析：{exc}") from exc
                    if isinstance(v, dict):
                        return v
                    break
    # 把原文片段带进错误信息：只报「找不到 JSON」的话，无法区分是
    # 「模型输出被 max_tokens 截断」还是「模型没按格式输出」。
    snippet = text[:300].replace("\n", " ")
    raise LlmError(f"模型返回内容里找不到 JSON 对象（原文前 300 字：{snippet}）")


SUMMARY_SYSTEM_PROMPT = """你是电力行业新闻分析师。给你一批电力新闻，请提炼核心要点与亮点。
只依据给定材料，不得引入材料外的信息。合并讲同一件事的多篇稿件。
每条要点是一句完整、可独立阅读的中文陈述句（不超过60字），具体到主体与动作，
避免"某公司召开会议"这类没有信息量的表述。
亮点挑选有实料的条目：含量化数据、有明确进展（投产/开工/并网/中标/首次）或政策口径变化。
source_key 只能使用材料中出现过的编号，不得编造。

只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏：
{"headline":"一句话总览","key_points":[{"text":"要点","source_keys":["a1"]}],
 "highlights":[{"text":"亮点","source_keys":["a2"]}]}"""


class LlmClient:
    """同步 LLM 客户端（服务端预生成用，阻塞可接受）。"""

    def __init__(self, settings: LlmSettings | None = None) -> None:
        self.settings = settings or settings_from_env()

    @property
    def configured(self) -> bool:
        return self.settings.ready

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        capability: str = "summarize",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """同步版本（脚本/测试用）。异步路径见 :meth:`achat_json`。"""
        return _run(self.achat_json(
            system=system, user=user, capability=capability, temperature=temperature,
        ))

    async def achat_json(
        self,
        *,
        system: str,
        user: str,
        capability: str = "summarize",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """发一次 chat 请求并返回解析后的 JSON。

        Raises:
            LlmError: 未配置、网络、鉴权、上游错误、响应不合法。
        """
        if not self.configured:
            raise LlmError(
                "未配置 LLM：需要 POWERNEWS_PLATFORM_LLM_BASE_URL / "
                "POWERNEWS_PLATFORM_LLM_API_KEY / POWERNEWS_PLATFORM_LLM_MODEL"
            )

        # 用户很可能直接粘贴带 /chat/completions 的完整地址，剥掉避免拼成双重路径
        base = self.settings.base_url.rstrip("/").removesuffix("/chat/completions")
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout) as client:
                resp = await client.post(
                    f"{base}/chat/completions", json=payload, headers=headers,
                )
        except httpx.HTTPError as exc:
            raise LlmError(f"调用 LLM 失败（网络）：{exc}") from exc

        if resp.status_code != 200:
            raise LlmError(
                f"调用 LLM 失败（HTTP {resp.status_code}）：{resp.text[:300]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise LlmError(f"LLM 响应不是合法 JSON：{exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"LLM 响应缺少 choices[0].message.content：{body}") from exc

        parsed = parse_lenient(str(content))
        missing = [k for k in REQUIRED_BY_CAPABILITY.get(capability, ()) if k not in parsed]
        if missing:
            raise LlmError(f"LLM 返回的 JSON 缺少必需字段 {missing}")
        return parsed
