"""邮件通知：以邮件方式传输登录二维码、验证码与告警信息。

设计要点（对应简历"以邮件方式传输其登录二维码、验证码，实现告警"）：

- :class:`EmailNotifier` 统一对外暴露 ``send_login_qr`` / ``send_captcha`` /
  ``send_alert`` 三个入口，主题与正文中附带二维码附件引用、验证码原文；
- 传输层为 :class:`SmtpTransport` Protocol，离线测试注入
  :class:`DryRunTransport`（把 .eml 风格报文写入 outbox 目录，不真正发信）
  或 :class:`RecordingTransport`（内存捕获，供断言）；
- :class:`SmtpTransportImpl` 为生产实现（smtplib 延迟导入），测试环境不会实例化，
  保证全程离线、无真实 SMTP 连接。
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Protocol, runtime_checkable

#: 邮件主题统一前缀，便于运维邮箱过滤。
SUBJECT_PREFIX = "[金融爬虫]"

#: 默认发件人占位（真实发送时由 SmtpTransportImpl 使用配置账号替换）。
DEFAULT_SENDER = "finance-crawler@localhost"


@dataclass(frozen=True)
class EmailMessage:
    """一封待发送邮件：主题、正文、收件人与附件（文件名, 字节）。"""

    subject: str
    body: str
    recipient: str
    #: 邮件类型：``login_qr`` / ``captcha`` / ``alert``
    kind: str = "alert"
    attachments: tuple[tuple[str, bytes], ...] = ()

    def render(self) -> str:
        """渲染为 .eml 风格文本（DryRun 落盘 / 断言用）。"""
        lines = [
            f"From: {DEFAULT_SENDER}",
            f"To: {self.recipient}",
            f"Subject: {self.subject}",
            f"Date: {formatdate(localtime=False)}",
            f"X-Kind: {self.kind}",
            "MIME-Version: 1.0",
            "Content-Type: text/plain; charset=utf-8",
            "",
            self.body,
        ]
        for filename, data in self.attachments:
            lines.append("")
            lines.append(f"---- ATTACHMENT: {filename} ({len(data)} bytes) ----")
        return "\n".join(lines) + "\n"


@runtime_checkable
class SmtpTransport(Protocol):
    """邮件传输协议：只依赖 ``send(message)``，离线测试注入假实现。"""

    def send(self, message: EmailMessage) -> None: ...


class DryRunTransport:
    """离线传输：把每封邮件写成 outbox 目录下的 .eml 文件 + 附件文件。

    不连接任何 SMTP 服务器；``outbox`` 下的产物即为"已发送邮件"的可审计副本。
    """

    def __init__(self, outbox: str | Path) -> None:
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        #: 已写出的全部文件路径（.eml 与附件）。
        self.sent: list[Path] = []
        #: 已发送邮件数（用于生成 001_/002_ 序号，与附件数解耦）。
        self.message_count = 0

    def send(self, message: EmailMessage) -> None:
        self.message_count += 1
        stem = f"{self.message_count:03d}_{message.kind}"
        eml_path = self.outbox / f"{stem}.eml"
        eml_path.write_text(message.render(), encoding="utf-8")
        self.sent.append(eml_path)
        for filename, data in message.attachments:
            att_path = self.outbox / f"{stem}__{filename}"
            att_path.write_bytes(data)
            self.sent.append(att_path)


class RecordingTransport:
    """内存捕获传输：记录全部已发送邮件，供测试断言。"""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)

class SmtpTransportImpl:
    """生产传输实现：通过 smtplib 发送（离线测试不实例化、不触发连接）。"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_ssl: bool = True,
        sender: str = DEFAULT_SENDER,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.sender = sender

    def send(self, message: EmailMessage) -> None:
        mime = MimeEmailMessage()
        mime["From"] = self.sender
        mime["To"] = message.recipient
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        for filename, data in message.attachments:
            mime.add_attachment(data, maintype="image", subtype="png", filename=filename)
        server_cls = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        with server_cls(self.host, self.port, timeout=30) as server:
            server.login(self.username, self.password)
            server.send_message(mime)


class EmailNotifier:
    """金融爬虫邮件通知器：二维码 / 验证码 / 告警的统一出口。

    参数
    ----
    transport:
        :class:`SmtpTransport` 实现（DryRun / Recording / Smtp）。
    sender / default_recipient:
        发件人与默认收件人（运维邮箱）。
    """

    def __init__(
        self,
        transport: SmtpTransport,
        *,
        sender: str = DEFAULT_SENDER,
        default_recipient: str = "ops@example.com",
    ) -> None:
        self.transport = transport
        self.sender = sender
        self.default_recipient = default_recipient
        #: 本通知器发出的全部邮件（便于断言，与 transport 捕获互补）。
        self.sent: list[EmailMessage] = []

    def send_login_qr(
        self,
        image: bytes | str | Path,
        *,
        filename: str = "login_qr.png",
        recipient: str | None = None,
    ) -> EmailMessage:
        """把站点登录二维码（PNG 字节或文件路径）以邮件附件形式发出。"""
        data = image if isinstance(image, bytes) else Path(image).read_bytes()
        message = EmailMessage(
            subject=f"{SUBJECT_PREFIX} 登录二维码（附件：{filename}，{len(data)} 字节）",
            body=(
                "目标站点需要扫码登录，请使用手机扫描附件二维码完成登录确认。\n"
                f"附件：{filename}（{len(data)} 字节，PNG）\n"
                "扫码成功后爬虫将自动继续执行；二维码有效期有限，请尽快处理。"
            ),
            recipient=recipient or self.default_recipient,
            kind="login_qr",
            attachments=((filename, data),),
        )
        return self._send(message)

    def send_captcha(self, code: str, *, recipient: str | None = None) -> EmailMessage:
        """把站点登录验证码以邮件正文形式发出，等待远程回复。"""
        message = EmailMessage(
            subject=f"{SUBJECT_PREFIX} 登录验证码：{code}",
            body=(
                "目标站点登录需要验证码，本次验证码为：\n"
                f"    {code}\n"
                "请将验证码回复到本邮件/运维通道，爬虫收到后将自动继续登录。"
            ),
            recipient=recipient or self.default_recipient,
            kind="captcha",
        )
        return self._send(message)

    def send_alert(
        self,
        subject: str,
        body: str,
        *,
        recipient: str | None = None,
        kind: str = "alert",
    ) -> EmailMessage:
        """发送告警邮件（主题自动加前缀）。"""
        message = EmailMessage(
            subject=f"{SUBJECT_PREFIX} 告警 {subject}",
            body=body,
            recipient=recipient or self.default_recipient,
            kind=kind,
        )
        return self._send(message)

    def _send(self, message: EmailMessage) -> EmailMessage:
        self.sent.append(message)
        self.transport.send(message)
        return message


__all__ = [
    "DEFAULT_SENDER",
    "DryRunTransport",
    "EmailMessage",
    "EmailNotifier",
    "RecordingTransport",
    "SmtpTransport",
    "SmtpTransportImpl",
    "SUBJECT_PREFIX",
]

