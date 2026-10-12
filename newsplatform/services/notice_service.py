"""公告栏服务：**服务端预生成 + 缓存 + 定时刷新**。

架构动机（对应需求 1 与 3）
--------------------------
原来的逻辑是「用户进入页面 → 前端调 LLM 实时生成」。三个后果：

1. **token 消耗与访客数成正比**。每个访客每次打开页面都是一次 LLM 调用，
   免费额度和费用都撑不住；
2. **内容不一致**。不同访客拿到的总结不同（模型有随机性），
   「公告栏」本该是同一份公告；
3. **定时刷新不生效**。前端 ``setInterval`` 只在页面开着时跑，
   用户关掉页面就停了 —— 而公告栏本该在任何时候都是新鲜的。

现在改成：

    服务器启动 → 预热生成一次 → 写入 Redis + 内存
    每 2 小时    → 后台任务重新生成，覆盖缓存
    用户访问     → GET /api/v1/notices 只读缓存（0 次 LLM 调用）

数据来源：本地采集库（``articles``）+ anysearch 实时搜索（补充覆盖）。
两条来源在响应里分别计数，**不把搜索结果伪装成本地采集数据**。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from newsagg.logutil import get_logger
from newsplatform.services.llm import SUMMARY_SYSTEM_PROMPT, LlmClient, LlmError
from newsplatform.services.news_search import (
    SearchDoc,
    asearch_electricity_news,
)

log = get_logger("newsplatform.notices")

#: 定时刷新间隔。默认 2 小时，与前端原间隔一致。
DEFAULT_INTERVAL_SECONDS = 2 * 60 * 60
#: 本地库参与总结的篇数上限。
LOCAL_ARTICLE_LIMIT = 40
#: anysearch 补充条数上限。
SEARCH_DOC_LIMIT = 18
#: 送进模型的材料字符上限。实测：71 条 × 400 字（≈24000 字符）会让推理型模型
#: 输出被截断而「找不到 JSON」；压到 12000 后稳定成功。
MAX_INPUT_CHARS = 12_000

REDIS_KEY = "powernews:notices:v1"

#: LLM 报错里出现这些片段即认定为「当日额度已耗尽」——当天不会再恢复，
#: 继续按原间隔重试只会白打请求并刷一堆错误日志。
_DAILY_QUOTA_HINTS = (
    "free-models-per-day", "per-day", "per_day", "daily limit", "daily quota",
)


def _is_daily_quota_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(h in text for h in _DAILY_QUOTA_HINTS)


def _tomorrow_midnight() -> datetime:
    """次日 0 点。额度类限制通常按自然日重置。"""
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=5, second=0, microsecond=0)


@dataclass(slots=True)
class NoticeBundle:
    """一次预生成的完整结果（缓存的最小单元）。"""

    headline: str
    key_points: list[dict[str, Any]]
    highlights: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    generated_at: str
    method: str
    sources: dict[str, Any]
    caveats: list[str] = field(default_factory=list)
    next_refresh_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "key_points": self.key_points,
            "highlights": self.highlights,
            "notices": self.notices,
            "generated_at": self.generated_at,
            "method": self.method,
            "sources": self.sources,
            "caveats": self.caveats,
            "next_refresh_at": self.next_refresh_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NoticeBundle:
        return cls(
            headline=str(data.get("headline") or ""),
            key_points=list(data.get("key_points") or []),
            highlights=list(data.get("highlights") or []),
            notices=list(data.get("notices") or []),
            generated_at=str(data.get("generated_at") or ""),
            method=str(data.get("method") or ""),
            sources=dict(data.get("sources") or {}),
            caveats=list(data.get("caveats") or []),
            next_refresh_at=str(data.get("next_refresh_at") or ""),
        )


class NoticeService:
    """公告栏预生成与缓存。

    单例（``get_notice_service``）。缓存两级：Redis 为主（跨进程、
    中台多副本时共享），进程内存为兜底（Redis 不可用时仍能秒回）。
    """

    def __init__(self) -> None:
        self._cache: NoticeBundle | None = None
        self._task: asyncio.Task | None = None
        self._interval = int(os.getenv("POWERNEWS_NOTICE_INTERVAL_SECONDS") or DEFAULT_INTERVAL_SECONDS)
        self._refreshing = False
        self._last_error = ""
        #: 暂停刷新的截止时刻。命中「日额度耗尽」时设到次日 0 点。
        self._paused_until: datetime | None = None

    @property
    def paused_until(self) -> str | None:
        if self._paused_until is None or datetime.now() >= self._paused_until:
            return None
        return self._paused_until.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- 读
    async def get(self) -> dict[str, Any]:
        """取公告数据。优先 Redis，回落内存；都没有则返回「尚未生成」。"""
        bundle = await self._load_from_redis() or self._cache
        if bundle is None:
            return {
                "ready": False,
                "reason": "公告尚未生成（服务刚启动，预热可能仍在进行）",
                "notices": [],
                "key_points": [],
                "highlights": [],
                "headline": "",
            }
        payload = bundle.to_dict()
        payload["ready"] = True
        return payload

    def status(self) -> dict[str, Any]:
        """给健康检查与调试用：缓存状态、定时任务状态。"""
        return {
            "cached": self._cache is not None,
            "generated_at": self._cache.generated_at if self._cache else None,
            "next_refresh_at": self._cache.next_refresh_at if self._cache else None,
            "interval_seconds": self._interval,
            "scheduler_running": self._task is not None and not self._task.done(),
            "refreshing": self._refreshing,
            "last_error": self._last_error,
            "llm_configured": LlmClient().configured,
            "paused_until": self.paused_until,
        }

    # ---------------------------------------------------------------- 生成
    async def refresh(self, *, reason: str = "manual") -> NoticeBundle:
        """重新生成并覆盖缓存。

        幂等且可重入：``_refreshing`` 防止定时任务与手动刷新叠在一起打两次 LLM。
        任何一步失败都**保留旧缓存** —— 公告栏展示稍旧的数据好过展示错误页。
        """
        if self._refreshing:
            log.info("公告刷新已在进行，跳过本次（reason=%s）", reason)
            return self._cache or await self._empty_bundle()
        self._refreshing = True
        try:
            bundle = await self._generate(reason)

            # ⚠️ 降级结果**不覆盖**有价值的旧缓存。
            #
            # 实测踩过：第 2 次刷新时 LLM 日额度耗尽，降级成「10 条标题」，
            # 直接把上一轮成功的「8 要点 + 5 亮点」覆盖掉了 —— 公告栏从 AI 公告
            # 退化成标题列表，而用户完全不知道为什么变差了。
            # 正确做法是**保留旧的成功结果**，只推迟下次刷新。
            if (
                bundle.method != "llm"
                and self._cache is not None
                and self._cache.method == "llm"
            ):
                log.warning(
                    "本次生成退化为 %s，保留上次 AI 结果不覆盖缓存（原因：%s）",
                    bundle.method,
                    "；".join(bundle.caveats)[:200] or "未知",
                )
                self._cache.next_refresh_at = bundle.next_refresh_at
                await self._save_to_redis(self._cache)
                return self._cache

            self._cache = bundle
            self._last_error = ""
            await self._save_to_redis(bundle)
            log.info(
                "公告已刷新（%s）：本地 %s 篇 + 检索 %s 条 → 要点 %s / 亮点 %s",
                reason,
                bundle.sources.get("local_articles"),
                bundle.sources.get("search_docs"),
                len(bundle.key_points),
                len(bundle.highlights),
            )
            return bundle
        except Exception as exc:  # noqa: BLE001 - 刷新失败不该让定时任务死掉
            self._last_error = str(exc)
            if _is_daily_quota_error(exc):
                # 当天不会再恢复：暂停到次日 0 点，避免无意义重试刷爆日志
                self._paused_until = _tomorrow_midnight()
                log.warning(
                    "LLM 当日额度已耗尽，暂停刷新到 %s（期间继续用现有缓存）",
                    self._paused_until.strftime("%Y-%m-%d %H:%M"),
                )
            log.error("公告刷新失败（%s）：%s", reason, exc)
            raise
        finally:
            self._refreshing = False

    async def _generate(self, reason: str) -> NoticeBundle:
        """生成一次公告（纯异步，不阻塞事件循环）。"""
        caveats: list[str] = []
        local_docs = await self._load_local_articles()
        search = await asearch_electricity_news()
        if not search.ok:
            caveats.append("anysearch 检索失败，本次仅使用本地采集库数据。")

        docs = local_docs + search.docs[:SEARCH_DOC_LIMIT]
        sources = {
            "local_articles": len(local_docs),
            "search_docs": len(search.docs),
            "search_queries": len(search.queries),
            "search_errors": search.errors,
        }
        if not docs:
            return await self._bundle_without_llm(
                caveats=["本地库与检索源均无可用数据。"],
                sources=sources, reason=reason,
            )

        client = LlmClient()
        if not client.configured:
            # 没配 LLM 时退化为「仅列标题」：公告栏仍有内容，且明确说明未经 AI 总结
            caveats.append("未配置 LLM，本次仅罗列新闻标题，未做要点总结。")
            return await self._bundle_without_llm(caveats=caveats, sources=sources, reason=reason)

        prompt = self._build_prompt(docs)
        try:
            raw = await client.achat_json(system=SUMMARY_SYSTEM_PROMPT, user=prompt)
        except LlmError as exc:
            caveats.append(f"LLM 调用失败（{exc}），本次退化为标题罗列。")
            # 额度类错误在 _generate 内部也要识别：它返回的是**降级结果**而不是
            # 抛异常，所以 refresh 的 except 分支根本不会执行 —— 只在那里设
            # `_paused_until` 的话，调度循环会继续按原间隔空转打请求。
            if _is_daily_quota_error(exc):
                self._paused_until = _tomorrow_midnight()
                log.warning(
                    "LLM 当日额度已耗尽，暂停刷新到 %s（期间继续用现有缓存）",
                    self._paused_until.strftime("%Y-%m-%d %H:%M"),
                )
            return await self._bundle_without_llm(caveats=caveats, sources=sources, reason=reason)

        known = {d.key: d for d in docs}
        key_points = self._clean_points(raw.get("key_points"), known, 8)
        highlights = self._clean_points(raw.get("highlights"), known, 5)

        bundle = NoticeBundle(
            headline=str(raw.get("headline") or "").strip(),
            key_points=key_points,
            highlights=highlights,
            notices=self._to_notices(key_points, highlights),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            method="llm",
            sources=sources,
            caveats=caveats,
        )
        bundle.next_refresh_at = self._next_at()
        return bundle

    # ---------------------------------------------------------------- 内部
    async def _load_local_articles(self) -> list[SearchDoc]:
        """从采集库取最近文章，归一成与搜索结果同形状。"""
        rows: list[dict[str, Any]] = []
        try:
            from newsplatform.core.database import get_database
            from newsplatform.repositories.articles import ArticleRepository

            db = get_database()
            # ⚠️ ``collector_session`` 是 ``async_sessionmaker``，用 ``async with``；
            # 写成 ``async for ... in`` 会报「requires __aiter__」（那是
            # ``get_collector_session`` 依赖的用法，两者长得像但不是一个东西）。
            async with db.collector_session() as session:
                repo = ArticleRepository(session)
                rows = await repo.list_for_summary(
                    self._recent_query(), limit=LOCAL_ARTICLE_LIMIT,
                )
        except Exception as exc:  # noqa: BLE001 - 本地库不可用不该让整次生成失败
            log.warning("读取本地采集库失败：%s", exc)
            return []

        out: list[SearchDoc] = []
        for i, row in enumerate(rows, 1):
            out.append(SearchDoc(
                key=f"a{i}",
                title=str(row.get("title") or ""),
                source=str(row.get("source_name") or ""),
                url=str(row.get("url") or ""),
                excerpt=str(row.get("body_text") or "")[:400],
                published=str(row.get("published_at_cst") or ""),
                origin="local",
            ))
        return out

    @staticmethod
    def _recent_query() -> Any:
        """近 7 天窗口 —— 与公告栏原有的默认档一致。"""
        from newsagg.timefilter import resolve_range
        from newsplatform.repositories.articles import ArticleQuery

        window = resolve_range("week")
        if window is None:
            return ArticleQuery()
        return ArticleQuery(date_from=window[0], date_to=window[1])

    @staticmethod
    def _build_prompt(docs: list[SearchDoc]) -> str:
        """拼材料。

        输入规模是实测调出来的：本地 35 篇 + 检索 36 条共 71 条、每条 400 字
        时，推理型模型会因思考过程吃掉大量 token 而**输出被截断**，
        表现是「找不到 JSON」。压到 12000 字符 + 每条 250 字后稳定成功。
        """
        blocks: list[str] = []
        used = 0
        for doc in docs:
            tag = "本地采集" if doc.origin == "local" else "实时检索"
            block = (
                f"\n[{doc.key}] {doc.title}\n"
                f"来源：{doc.source or '未知'}（{tag}）  时间：{doc.published or '未知'}\n"
                f"{doc.excerpt[:250]}\n"
            )
            if used + len(block) > MAX_INPUT_CHARS:
                break
            blocks.append(block)
            used += len(block)
        header = f"材料共 {len(docs)} 条电力新闻（本地采集 + 实时检索），此处提供 {len(blocks)} 条。\n"
        return header + "".join(blocks)

    @staticmethod
    def _clean_points(raw: Any, known: dict[str, SearchDoc], cap: int) -> list[dict[str, Any]]:
        """清洗模型给的要点，**过滤编造的 source_keys**。

        语义与前端 ``ai-llm.js`` 的 ``cleanPoints`` 一致：模型会编造引用，
        只保留材料里真实存在的 key；引用全无效时降级为「无出处要点」而不是丢弃。
        """
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw[:cap]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            keys_raw = item.get("source_keys")
            keys: list[str] = []
            if isinstance(keys_raw, list):
                for value in keys_raw:
                    k = str(value)
                    if k in known and k not in keys:
                        keys.append(k)
            out.append({
                "text": text,
                "article_ids": [],
                "source_keys": keys,
                # 去重：同一条要点常引用同一站点的多篇稿，不去重会出现
                # 「来源：[中国电力新闻网, 中国电力新闻网]」这种噪音
                "sources": list(dict.fromkeys(
                    known[k].source for k in keys if known[k].source
                )),
                "urls": list(dict.fromkeys(known[k].url for k in keys if known[k].url)),
                "reasons": [] if keys else ["模型未给出有效出处"],
            })
        return out

    @staticmethod
    def _to_notices(
        key_points: list[dict[str, Any]],
        highlights: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """要点/亮点 → 公告条目。置顶规则与前端 ``utils/notices.js`` 一致：
        亮点优先、含量化特征的置顶。"""
        out: list[dict[str, Any]] = []
        for p in highlights:
            out.append({
                "id": f"hl-{len(out)}", "kind": "highlight",
                "pinned": True, "pinReason": "亮点",
                "text": p["text"], "sources": p.get("sources") or [],
                "urls": p.get("urls") or [], "reasons": p.get("reasons") or [],
            })
        for p in key_points:
            out.append({
                "id": f"kp-{len(out)}", "kind": "keypoint",
                "pinned": False, "pinReason": "",
                "text": p["text"], "sources": p.get("sources") or [],
                "urls": p.get("urls") or [], "reasons": p.get("reasons") or [],
            })
        return out

    async def _bundle_without_llm(
        self,
        *,
        caveats: list[str],
        sources: dict[str, Any],
        reason: str,
    ) -> NoticeBundle:
        """无 LLM 时的降级产物：罗列标题，明确标注未经总结。

        降级路径也要给用户**能看的东西**：空公告栏比「标题列表 + 一句说明」差得多。
        """
        docs = (await self._load_local_articles())[:10]
        notices = [
            {
                "id": f"t-{i}", "kind": "title", "pinned": False, "pinReason": "",
                "text": d.title, "sources": [d.source] if d.source else [],
                "urls": [d.url] if d.url else [], "reasons": [],
            }
            for i, d in enumerate(docs)
        ]
        bundle = NoticeBundle(
            headline="",
            key_points=[], highlights=[], notices=notices,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            method="title-only",
            sources=sources,
            caveats=caveats,
        )
        bundle.next_refresh_at = self._next_at()
        return bundle

    async def _empty_bundle(self) -> NoticeBundle:
        return NoticeBundle(
            headline="", key_points=[], highlights=[], notices=[],
            generated_at="", method="none", sources={}, caveats=["尚无数据"],
        )

    def _next_at(self) -> str:
        from datetime import timedelta

        return (datetime.now() + timedelta(seconds=self._interval)).strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- Redis
    async def _load_from_redis(self) -> NoticeBundle | None:
        try:
            import redis.asyncio as aioredis

            url = os.getenv("POWERNEWS_PLATFORM_REDIS_URL") or "redis://127.0.0.1:6380/1"
            client = aioredis.from_url(url, decode_responses=True)
            try:
                raw = await client.get(REDIS_KEY)
            finally:
                await client.aclose()
            if not raw:
                return None
            return NoticeBundle.from_dict(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 - Redis 不可用就回落内存
            log.debug("读取 Redis 公告缓存失败（回落内存）：%s", exc)
            return None

    async def _save_to_redis(self, bundle: NoticeBundle) -> None:
        try:
            import redis.asyncio as aioredis

            url = os.getenv("POWERNEWS_PLATFORM_REDIS_URL") or "redis://127.0.0.1:6380/1"
            client = aioredis.from_url(url, decode_responses=True)
            try:
                # TTL 比刷新间隔长一倍：即使定时任务挂了，缓存也不会立刻消失
                await client.set(
                    REDIS_KEY,
                    json.dumps(bundle.to_dict(), ensure_ascii=False),
                    ex=self._interval * 2,
                )
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 写不进去不影响内存缓存
            log.debug("写入 Redis 公告缓存失败（仅用内存）：%s", exc)

    # ---------------------------------------------------------------- 调度
    async def start_scheduler(self) -> None:
        """启动定时刷新 + 首次预热。

        在 FastAPI lifespan 里调用。预热**不阻塞启动**：用 ``create_task``
        丢到后台，服务先就绪，公告栏首访可能拿到「尚未生成」，
        但那好过让所有端点等一次 LLM 调用（4~15 秒）。
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._scheduler_loop(), name="notice-scheduler")
        log.info("公告定时任务已启动：每 %d 秒刷新一次", self._interval)

    async def _scheduler_loop(self) -> None:
        # 启动即预热一次
        try:
            await self.refresh(reason="startup")
        except Exception:  # noqa: BLE001 - 首刷失败不该终止调度
            pass
        while True:
            # 额度耗尽时睡到解禁时刻，而不是按原间隔空转重试
            paused = self._paused_until
            if paused is not None and datetime.now() < paused:
                wait = max(60.0, (paused - datetime.now()).total_seconds())
                log.info("刷新暂停中，%.0f 分钟后重试", wait / 60)
                await asyncio.sleep(wait)
                self._paused_until = None
                continue

            await asyncio.sleep(self._interval)
            try:
                await self.refresh(reason="schedule")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单次失败不该终止循环
                # 失败后按原间隔继续，下一轮再试；缓存里仍是上一次的成功结果
                continue

    async def stop_scheduler(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


_NOTICE_SERVICE: NoticeService | None = None


def get_notice_service() -> NoticeService:
    """进程级单例。"""
    global _NOTICE_SERVICE
    if _NOTICE_SERVICE is None:
        _NOTICE_SERVICE = NoticeService()
    return _NOTICE_SERVICE
