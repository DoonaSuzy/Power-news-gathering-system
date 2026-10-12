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
            # socket_timeout **必须大于 XREADGROUP 的阻塞窗口**（默认 5s），
            # 否则阻塞还没结束，socket 读先超时了 —— 表现为 worker 日志里
            # 刷屏的 "Timeout reading from socket"，且积压消息得不到认领。
            # 留 10s 余量：BLOCK 5s 正常返回，真断连也能在 15s 内发现。
            self._client = redis.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                protocol=2,
                socket_timeout=15,
                socket_connect_timeout=5,
                health_check_interval=30,
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
        """读取新消息，返回 ``[(message_id, job_id)]``。

        ⚠️ **阻塞读超时不算错误** —— 它就是「这段时间没有新消息」，
        必须返回空列表而不是抛异常。理由：
        调用方（worker）在 ``take()`` 返回空后才会去 ``reclaim_stale()``
        认领积压消息。若超时抛异常，异常会冒泡到主循环被吞掉，
        **reclaim 永远不会执行**，PEL 里的消息就会无限积压。

        （84 生产环境实测：109 条消息被卡 2 小时，就是这个原因 ——
        ``lag=0`` 时每次 XREADGROUP 都超时，worker 反复"业务异常，继续"，
        但从不认领积压。）

        Raises:
            QueueError: 仅针对**真错误**（连接断开、协议错误等）。
        """
        self.ensure_group()
        try:
            resp = self.client.xreadgroup(
                groupname=self.group, consumername=consumer,
                streams={self.stream: ">"}, count=count, block=block_ms,
            )
        except TimeoutError:
            # 阻塞窗口内没有新消息 —— 正常情况，返回空让调用方去 reclaim
            return []
        except Exception as exc:  # noqa: BLE001
            # redis-py 把底层 socket 超时包装成 redis.exceptions.TimeoutError，
            # 而它在 `TimeoutError` 的继承链上不一定是内置 TimeoutError，
            # 所以这里再用消息特征兜一层，避免同类问题换个异常类型又漏掉。
            if "timeout" in str(exc).lower():
                log.debug("XREADGROUP 阻塞超时（无新消息）: %s", exc)
                return []
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

    def delivery_count(self, message_id: str) -> int | None:
        """查一条消息被投递过几次（即已重试几次）。

        数据来自 Redis Stream 的 ``XPENDING``（``times_delivered``），
        **不依赖数据库加列** —— 重试次数本就是队列的属性，
        为它改 DDL 会把「队列关注点」泄漏到 schema 里。

        Args:
            message_id: Stream 消息 id。

        Returns:
            投递次数；消息已被 ack 或查询失败时返回 ``None``
            （调用方应把 ``None`` 当作「无法判断」，而非 0）。
        """
        try:
            pendings = self.client.xpending_range(
                self.stream, self.group, min=message_id, max=message_id, count=1,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("XPENDING 失败 %s: %s", message_id, exc)
            return None
        for item in pendings or []:
            mid = item.get("message_id") if isinstance(item, dict) else None
            if mid is not None and str(mid) == message_id:
                return int(item.get("times_delivered") or 0)
        return None

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
