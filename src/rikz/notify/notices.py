"""Who gets told what.

* Shareholders: one e-mail per new report version with the shareholder link,
  the report date and how many things changed. No figures and no
  attachments: the report stays behind the access key even if the e-mail is
  forwarded. The access key is never sent by e-mail.
* Admin: every new report (with the changes list), every rejected upload
  (with the reasons), Drive errors and jobs that failed for good.

RIKZ_SHAREHOLDER_EMAIL_MODE=review (default) waits for the admin to press
"Send to shareholders"; =auto sends as soon as a report is created.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass

from sqlalchemy import select

from ..ingest.worker import enqueue, handler
from ..store.db import Notification, Snapshot, now


@dataclass(frozen=True)
class NoticeConfig:
    shareholders: tuple[str, ...]
    admins: tuple[str, ...]
    mode: str  # review | auto
    view_link: str
    admin_link: str

    @classmethod
    def from_env(cls, view_link: str, admin_link: str) -> "NoticeConfig":
        def emails(var):
            return tuple(e.strip() for e in os.environ.get(var, "").split(",") if e.strip())

        mode = os.environ.get("RIKZ_SHAREHOLDER_EMAIL_MODE", "review")
        if mode not in ("review", "auto"):
            raise RuntimeError("RIKZ_SHAREHOLDER_EMAIL_MODE must be review or auto")
        return cls(emails("RIKZ_SHAREHOLDER_EMAILS"), emails("RIKZ_ADMIN_EMAILS"), mode, view_link, admin_link)


def _queue(store, audience: str, kind: str, to: str, subject: str, text: str, body_html: str,
           version: int | None = None) -> None:
    with store.Session() as s:
        n = Notification(audience=audience, kind=kind, snapshot_version=version, recipient=to, subject=subject)
        s.add(n)
        s.commit()
        nid = n.id
    job = enqueue(store.Session, "email.send", {"notification_id": nid, "to": to, "subject": subject,
                                                "text": text, "html": body_html})
    with store.Session() as s:
        s.get(Notification, nid).job_id = job
        s.commit()


def _page(title: str, paras: list[str], link: tuple[str, str] | None = None, items: list[str] | None = None) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paras)
    if items:
        body += "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>"
    if link:
        body += (f'<p><a href="{html.escape(link[1])}" style="display:inline-block;background:#1f5f8b;color:#fff;'
                 f'padding:10px 16px;border-radius:6px;text-decoration:none">{html.escape(link[0])}</a></p>')
    return (f'<div style="font-family:Arial,sans-serif;font-size:15px;color:#17202a;max-width:560px">'
            f"<h2 style=\"font-size:18px\">{html.escape(title)}</h2>{body}"
            f'<p style="color:#5d6b7a;font-size:12px">Rikz portfolio reporting – automated message.</p></div>')


def queue_shareholder_report(store, cfg: NoticeConfig, version: int, *, resend: bool = False) -> int:
    """Queue the report notice to every shareholder. Returns how many were queued."""
    with store.Session() as s:
        snap = s.scalars(select(Snapshot).where(Snapshot.version == version)).first()
        if snap is None:
            raise ValueError(f"no report version {version}")
        already = {n.recipient for n in s.scalars(select(Notification).where(
            Notification.audience == "shareholder", Notification.snapshot_version == version,
            Notification.status != "failed"))}
        as_of, n_changes = snap.as_of, len(snap.changes)
    subject = f"Rikz portfolio report – as of {as_of:%d %b %Y}"
    paras = [f"A new portfolio report (version {version}) is available, as of {as_of:%d %B %Y}.",
             f"It lists {n_changes} change{'s' if n_changes != 1 else ''} since the previous report.",
             "Open it with your access key. The key is not sent by e-mail."]
    text = "\n\n".join(paras) + f"\n\n{cfg.view_link}\n"
    body = _page("New portfolio report", paras, ("Open the report", cfg.view_link))
    count = 0
    for to in cfg.shareholders:
        if to in already and not resend:
            continue
        _queue(store, "shareholder", "report", to, subject, text, body, version)
        count += 1
    return count


def make_listener(store, cfg: NoticeConfig):
    def on_result(result, source: str) -> None:
        if result.status == "rejected":
            subject = "Rikz: an upload was rejected"
            paras = [f"An upload from {source} was rejected; nothing from it was imported.", "Reasons:"]
            text = "\n".join([paras[0], "", paras[1], *[f"- {r}" for r in result.reasons], "", cfg.admin_link])
            body = _page("Upload rejected", paras, ("Open the admin page", cfg.admin_link + "admin"), result.reasons)
            for to in cfg.admins:
                _queue(store, "admin", "rejected", to, subject, text, body)
            return
        if result.snapshot_version:
            v = result.snapshot_version
            changes = [c["text"] for c in result.changes]
            review = cfg.mode == "review" and cfg.shareholders
            subject = f"Rikz: report v{v} created" + (" – review and send" if review else "")
            paras = [f"Report version {v} was created from a {source} upload."]
            if review:
                paras.append("Shareholders have not been told yet. Review it, then press "
                             "\"Send to shareholders\" on the admin page.")
            elif cfg.shareholders:
                paras.append(f"The shareholder notice has been queued for {len(cfg.shareholders)} recipient(s).")
            paras.append("Changes since the previous report:")
            text = "\n".join([*paras, *[f"- {c}" for c in changes], "", cfg.admin_link + f"?v={v}"])
            body = _page(f"Report v{v} created", paras, ("Review the report", cfg.admin_link + f"?v={v}"), changes)
            for to in cfg.admins:
                _queue(store, "admin", "report", to, subject, text, body, v)
            if cfg.mode == "auto":
                queue_shareholder_report(store, cfg, v)

    return on_result


def notify_admins(store, cfg: NoticeConfig, kind: str, subject: str, message: str) -> None:
    for to in cfg.admins:
        _queue(store, "admin", kind, to, subject, message + "\n\n" + cfg.admin_link + "admin",
               _page(subject, [message], ("Open the admin page", cfg.admin_link + "admin")))


@handler("email.send")
def send_email(store, payload: dict, context: dict) -> None:
    mailer = context.get("mailer")
    if mailer is None:
        raise RuntimeError("e-mail is not configured (set SMTP_HOST and related variables)")
    mailer.send(payload["to"], payload["subject"], payload["text"], payload.get("html"))
    with store.Session() as s:
        n = s.get(Notification, payload["notification_id"])
        if n is not None:
            n.status, n.sent_at = "sent", now()
            s.commit()


def mark_failed_notifications(store) -> list[str]:
    """Notifications whose job gave up: mark them failed; return their subjects."""
    from ..store.db import Job

    out = []
    with store.Session() as s:
        for n in s.scalars(select(Notification).where(Notification.status == "queued")):
            job = s.get(Job, n.job_id) if n.job_id else None
            if job is not None and job.status == "failed":
                n.status = "failed"
                out.append(f"{n.subject} → {n.recipient}")
        s.commit()
    return out
