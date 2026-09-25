"""SMTP sending. All settings come from environment variables."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    sender: str
    sender_name: str
    security: str  # starttls | ssl | none

    @classmethod
    def from_env(cls) -> "MailConfig | None":
        host = os.environ.get("SMTP_HOST")
        if not host:
            return None
        security = os.environ.get("SMTP_SECURITY", "starttls").lower()
        if security not in ("starttls", "ssl", "none"):
            raise RuntimeError("SMTP_SECURITY must be starttls, ssl or none")
        return cls(
            host=host,
            port=int(os.environ.get("SMTP_PORT", "465" if security == "ssl" else "587")),
            user=os.environ.get("SMTP_USER") or None,
            password=os.environ.get("SMTP_PASSWORD") or None,
            sender=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "",
            sender_name=os.environ.get("SMTP_FROM_NAME", "Rikz Portfolio Reporting"),
            security=security,
        )


def build_message(cfg: MailConfig, to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class SMTPMailer:
    def __init__(self, cfg: MailConfig):
        self.cfg = cfg

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        cfg = self.cfg
        msg = build_message(cfg, to, subject, text, html)
        ctx = ssl.create_default_context()
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
        with server:
            if cfg.security == "starttls":
                server.starttls(context=ctx)
            if cfg.user:
                server.login(cfg.user, cfg.password or "")
            server.send_message(msg)
