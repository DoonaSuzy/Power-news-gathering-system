"""告警与远程恢复：失败告警记录 + 邮件通知 + 远程下发恢复命令。

对应简历"实现告警并可远程恢复其爬虫服务"：

- :class:`AlertManager.on_failure` 在采集失败时**立即**创建带时间戳的
  :class:`Alert`（``created_at`` 即失败时刻），并通过
  :class:`~newsagg.finance.emailer.EmailNotifier` 发送告警邮件；
- :data:`ALERT_SLA_SECONDS`（300 秒）约定告警时效 SLA，
  :meth:`AlertManager.is_within_sla` 用于校验告警是否在 SLA 内发出；
- :meth:`AlertManager.register_recovery_handler` 注册本地恢复处理器，
  :meth:`AlertManager.remote_recover` 构造 :class:`RecoveryCommand` 并同时
  派发给 :class:`RecoveryTransport`（生产为远程控制通道，测试为
  :class:`RecordingRecoveryTransport` 内存捕获）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from newsagg.finance.emailer import EmailNotifier

#: 告警 SLA：失败发生后 5 分钟内必须完成告警（含邮件送达）。
ALERT_SLA_SECONDS = 300.0

Clock = Callable[[], float]


@dataclass(frozen=True)
class Alert:
    """一条告警记录：来源、错误、级别与创建时刻。"""

    source: str
    error: str
    level: str = "critical"
    created_at: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryCommand:
    """一条远程恢复命令（如 restart / refresh_auth）。"""

    source: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    issued_at: float = 0.0


@runtime_checkable
class RecoveryTransport(Protocol):
    """恢复命令传输协议：把 :class:`RecoveryCommand` 下发到远程执行端。"""

    def dispatch(self, command: RecoveryCommand) -> None: ...


class RecordingRecoveryTransport:
    """离线恢复传输：内存捕获全部下发的恢复命令，供测试断言。"""

    def __init__(self) -> None:
        self.commands: list[RecoveryCommand] = []

    def dispatch(self, command: RecoveryCommand) -> None:
        self.commands.append(command)


class RemoteRecoveryTransport:
    """生产恢复传输：把命令发布到控制通道（Redis 队列 / 内部 API 等）。

    ``publisher`` 为可注入的发布函数（如 ``redis.publish`` 的包装）；
    未提供时拒绝下发，避免离线环境误连真实通道。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        channel: str = "finance:recover",
        publisher: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.channel = channel
        self._publisher = publisher

    def dispatch(self, command: RecoveryCommand) -> None:
        if self._publisher is None:
            raise RuntimeError(
                "RemoteRecoveryTransport 需要注入 publisher（离线测试请使用 "
                "RecordingRecoveryTransport）"
            )
        self._publisher(
            self.channel,
            {
                "source": command.source,
                "action": command.action,
                "payload": command.payload,
                "issued_at": command.issued_at,
                "endpoint": self.endpoint,
            },
        )


#: 恢复处理器：接收 :class:`RecoveryCommand`，返回是否处理成功。
RecoveryHandler = Callable[[RecoveryCommand], bool]


class AlertManager:
    """告警记录 + 邮件通知 + 远程恢复命令下发。"""

    def __init__(
        self,
        *,
        notifier: EmailNotifier | None = None,
        recovery_transport: RecoveryTransport | None = None,
        clock: Clock | None = None,
        sla_seconds: float = ALERT_SLA_SECONDS,
        recipient: str | None = None,
    ) -> None:
        self._notifier = notifier
        self._recovery_transport = recovery_transport
        self._clock = clock or time.time
        self.sla_seconds = sla_seconds
        self._recipient = recipient
        #: 全部告警（按时间顺序）。
        self.alerts: list[Alert] = []
        #: 全部已下发的恢复命令。
        self.recoveries: list[RecoveryCommand] = []
        self._handlers: dict[str, RecoveryHandler] = {}

    # ------------------------------------------------------------------ 告警

    def on_failure(
        self,
        source: str,
        error: Exception | str,
        *,
        level: str = "critical",
        detail: dict[str, Any] | None = None,
    ) -> Alert:
        """采集失败入口：立即记录告警并通过邮件通知（满足 SLA）。"""
        now = self._clock()
        alert = Alert(
            source=source,
            error=str(error),
            level=level,
            created_at=now,
            detail=dict(detail or {}),
        )
        self.alerts.append(alert)
        if self._notifier is not None:
            self._notifier.send_alert(
                f"{source} 采集失败",
                (
                    f"来源：{source}\n"
                    f"级别：{level}\n"
                    f"时间：{now:.0f}\n"
                    f"错误：{alert.error}\n"
                    f"详情：{alert.detail or '{}'}\n"
                    f"告警 SLA：{self.sla_seconds:.0f}s 内发出。"
                ),
                recipient=self._recipient,
            )
        return alert

    def is_within_sla(self, alert: Alert, *, now: float | None = None) -> bool:
        """校验告警发出时间是否满足 SLA（默认 5 分钟）。"""
        reference = now if now is not None else self._clock()
        return 0.0 <= reference - alert.created_at <= self.sla_seconds

    # ------------------------------------------------------------------ 恢复

    def register_recovery_handler(self, name: str, handler: RecoveryHandler) -> None:
        """注册恢复处理器：``name`` 为来源名或 ``"*"``（通配）。"""
        self._handlers[name] = handler

    def remote_recover(
        self,
        source: str,
        *,
        action: str = "restart",
        payload: dict[str, Any] | None = None,
    ) -> RecoveryCommand:
        """远程恢复入口：构造并下发恢复命令，同时触发本地处理器。"""
        command = RecoveryCommand(
            source=source,
            action=action,
            payload=dict(payload or {}),
            issued_at=self._clock(),
        )
        self.recoveries.append(command)
        handler = self._handlers.get(source) or self._handlers.get("*")
        if handler is not None:
            handler(command)
        if self._recovery_transport is not None:
            self._recovery_transport.dispatch(command)
        return command


__all__ = [
    "ALERT_SLA_SECONDS",
    "Alert",
    "AlertManager",
    "Clock",
    "RecoveryCommand",
    "RecoveryHandler",
    "RecoveryTransport",
    "RecordingRecoveryTransport",
    "RemoteRecoveryTransport",
]

