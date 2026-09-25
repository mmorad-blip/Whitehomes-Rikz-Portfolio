"""Hardening for the website: security headers, token redaction in logs,
request size limit, and throttling of access-key guesses."""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

from sqlalchemy import delete, func, select

from ..store.db import LoginAttempt, now

log = logging.getLogger("rikz.web")

CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
TOKEN_PATH = re.compile(r"^/(v|a)/[^/]+")

# Guessing limits. Keys carry ~100 bits, so these are defence in depth.
CLIENT_MAX_FAILURES, CLIENT_WINDOW = 5, timedelta(minutes=15)
ROLE_MAX_FAILURES, ROLE_WINDOW = 50, timedelta(hours=1)


def redact(path: str) -> str:
    """Paths carry the private link token; logs get a placeholder instead."""
    return TOKEN_PATH.sub(lambda m: f"/{m.group(1)}/<token>", path)


class SecurityMiddleware:
    """Pure ASGI middleware: headers on every response, a cap on request
    bodies, and one redacted access-log line per request."""

    def __init__(self, app, *, max_body: int, hsts: bool):
        self.app, self.max_body, self.hsts = app, max_body, hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status_holder = {"status": 500}
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body:
            await _plain(send, 413, "Upload too large.")
            log.info("%s %s 413", scope["method"], redact(scope["path"]))
            return

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body:
                    raise _TooLarge()
            return message

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                extra = {
                    b"content-security-policy": CSP.encode(),
                    b"x-frame-options": b"DENY",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), interest-cohort=()",
                    b"x-robots-tag": b"noindex, nofollow",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"cache-control": b"no-store",
                }
                if self.hsts:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra.items() if k not in have]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _TooLarge:
            await _plain(send, 413, "Upload too large.")
            status_holder["status"] = 413
        finally:
            log.info("%s %s %s %.0fms", scope["method"], redact(scope["path"]), status_holder["status"],
                     (time.monotonic() - started) * 1000)


class _TooLarge(Exception):
    pass


async def _plain(send, status: int, text: str) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": text.encode()})


def locked_out(Session, role: str, client: str) -> str | None:
    """A reason when this visitor (or everyone, for this link) must wait."""
    t = now()
    with Session() as s:
        mine = s.scalar(select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.role == role, LoginAttempt.client == client, LoginAttempt.ok.is_(False),
            LoginAttempt.at > t - CLIENT_WINDOW))
        everyone = s.scalar(select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.role == role, LoginAttempt.ok.is_(False), LoginAttempt.at > t - ROLE_WINDOW))
    if mine >= CLIENT_MAX_FAILURES:
        return "Too many wrong keys from this device. Try again in 15 minutes."
    if everyone >= ROLE_MAX_FAILURES:
        return "Sign-in to this link is paused after many wrong keys. Try again within the hour."
    return None


def record_attempt(Session, role: str, client: str, ok: bool) -> None:
    with Session() as s:
        s.add(LoginAttempt(role=role, client=client, ok=ok))
        if ok:  # a success clears this visitor's failures
            s.execute(delete(LoginAttempt).where(LoginAttempt.role == role, LoginAttempt.client == client,
                                                 LoginAttempt.ok.is_(False)))
        s.commit()


def purge_old(Session, *, views_days: int = 400, attempts_days: int = 30) -> None:
    """Keep the view log for a bit over a year and sign-in attempts for a month."""
    from ..store.db import ViewLog

    t = now()
    with Session() as s:
        s.execute(delete(ViewLog).where(ViewLog.at < t - timedelta(days=views_days)))
        s.execute(delete(LoginAttempt).where(LoginAttempt.at < t - timedelta(days=attempts_days)))
        s.commit()
