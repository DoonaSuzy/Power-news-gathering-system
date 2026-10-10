"""Redis Stream 任务队列封装。

流结构（与 ``.env`` 的 ``POWERNEWS_QUEUE_STREAM`` 对应）::

    powernews:site_jobs      # 流名
    powernews-workers        # 消费组

消息体只放 ``{"job_id": "123"}``，具体任务详情一律回库读——
避免队列里出现会过期的业务快照。
"""

from __future__ import annotations

import json
from typing import Any

from newsagg.config import Settings, get_settings
from newsagg.errors import QueueError
from newsagg.logutil import get_logger

log = get_logger("queue")

__all__ = ["StreamQueue"]


class StreamQueue:
    """Redis Stream 队列（生产 / 消费 / ack）。"""

    def __init__(self, settings: Settings | None = None, client=None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    # ---------------- 连接 ----------------
    @property
    def client(self):
        """惰性建立 redis 客户端。

        固定 ``protocol=2``（RESP2）。redis-py 8.x 默认走 RESP3，连接时会先发
        ``HELLO 3``，而**旧版 Redis/Memurai 不认识 HELLO**，会直接抛
        ``unknown command 'HELLO'`` 让队列整体不可用。RESP2 是所有版本都支持的
        公共子集，且本项目只用到 ``XADD/XREADGROUP/XAUTOCLAIM/XACK`` 这几个
        在 RESP2 下语义完全一致的命令，没有理由为 RESP3 付出兼容性代价。
        """
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover
                raise QueueError("缺少 redis 依赖，请先 pip install -r requirements.txt") from exc
            self._client = redis.Redis.from_url(
                self.settings.redis_url, decode_responses=True, protocol=2
            )
        return self._client

    @property
    def stream(self) -> str:
        return self.settings.queue_stream

    @property
    def group(self) -> str:
        return self.settings.queue_group

    def ping(self) -> bool:
        """连通性检查。"""
        try:
            return bool(self.client.ping())
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis 不可用: %s", exc)
            return False

    # ---------------- 生产 ----------------
    def ensure_group(self) -> None:
        """确保消费组存在（幂等）。"""
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, job_ids: list[int]) -> int:
        """批量投递任务，返回成功投递条数。"""
        if not job_ids:
            return 0
        self.ensure_group()
        pipe = self.client.pipeline()
        for job_id in job_ids:
            pipe.xadd(self.stream, {"job_id": str(job_id)})
        results = pipe.execute()
        published = sum(1 for r in results if r)
        log.info("投递任务 %d 条", published)
        return published

    # ---------------- 消费 ----------------
    def take(self, *, consumer: str, block_ms: int = 5000, count: int = 1,
             ) -> list[tuple[str, int]]:
        """读取新消息，返回 ``[(message_id, job_id)]``。"""
        self.ensure_group()
        try:
            resp = self.client.xreadgroup(
                groupname=self.group, consumername=consumer,
                streams={self.stream: ">"}, count=count, block=block_ms,
            )
        except Exception as exc:  # noqa: BLE001
            raise QueueError(f"XREADGROUP 失败: {exc}") from exc

        out: list[tuple[str, int]] = []
        for _stream, entries in resp or []:
            for message_id, fields in entries:
                try:
                    job_id = int(fields.get("job_id", "0"))
                except (TypeError, ValueError):
                    continue
                if job_id:
                    out.append((message_id, job_id))
        return out

    def reclaim_stale(self, *, consumer: str, min_idle_ms: int = 600_000,
                      count: int = 10) -> list[tuple[str, int]]:
        """认领超时未 ack 的消息（worker 崩溃后任务不丢）。"""
        try:
            resp = self.client.xautoclaim(
                self.stream, self.group, consumer,
                min_idle_time=min_idle_ms, start_id="0-0", count=count,
            )
        except Exception:  # noqa: BLE001
            return []
        entries = resp[1] if isinstance(resp, (list, tuple)) and len(resp) > 1 else []
        out: list[tuple[str, int]] = []
        for message_id, fields in entries or []:
            try:
                out.append((message_id, int(fields.get("job_id", "0"))))
            except (TypeError, ValueError):
                continue
        return out

    def ack(self, message_id: str) -> None:
        """确认消息。"""
        try:
            self.client.xack(self.stream, self.group, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("XACK 失败 %s: %s", message_id, exc)

    # ---------------- 运维 ----------------
    def stats(self) -> dict[str, Any]:
        """队列概览（积压 / 待处理 / 消费者）。"""
        try:
            info = self.client.xinfo_groups(self.stream)
        except Exception:  # noqa: BLE001
            return {"stream": self.stream, "length": 0, "groups": []}
        return {
            "stream": self.stream,
            "length": int(self.client.xlen(self.stream)),
            "groups": [
                {
                    "name": g.get("name"),
                    "consumers": g.get("consumers"),
                    "pending": g.get("pending"),
                    "last_delivered_id": g.get("last-delivered-id"),
                }
                for g in info
            ],
        }

    def reset_group(self) -> None:
        """删除消费组（运维用：重置队列状态）。"""
        try:
            self.client.xgroup_destroy(self.stream, self.group)
        except Exception:  # noqa: BLE001
            pass

    def dump_message(self, message_id: str) -> dict[str, Any] | None:
        """读取单条消息原文（调试用）。"""
        try:
            entries = self.client.xrange(self.stream, message_id, message_id)
        except Exception:  # noqa: BLE001
            return None
        if not entries:
            return None
        _mid, fields = entries[0]
        return dict(fields)

    @staticmethod
    def encode(fields: dict[str, Any]) -> dict[str, str]:
        """把任意值编码为 Stream 可接受的字符串字段。"""
        return {k: json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                for k, v in fields.items()}
