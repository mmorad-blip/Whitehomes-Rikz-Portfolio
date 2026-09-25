from email import message_from_bytes

import pytest
from sqlalchemy import select

from conftest import AWAED, MANAFA
from rikz.ingest.worker import run_jobs
from rikz.notify import mail as mailmod
from rikz.notify.notices import (NoticeConfig, make_listener, mark_failed_notifications, queue_shareholder_report)
from rikz.store.db import Job, Notification, init_db, make_engine
from rikz.store.files import FileStore
from rikz.store.service import Store

VIEW_LINK, ADMIN_LINK = "https://report.example/v/tok-view/", "https://report.example/a/tok-admin/"


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text, html=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})


def cfg(mode="review"):
    return NoticeConfig(("a@example.com", "b@example.com"), ("admin@example.com",), mode, VIEW_LINK, ADMIN_LINK)


@pytest.fixture
def store(tmp_path):
    return Store(init_db(make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")), FileStore(tmp_path / "files"))


def full_upload():
    paths = [*sorted(AWAED.glob("*.pdf")), MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]
    return [(p.name, p.read_bytes()) for p in paths]


def notices(store, **where):
    with store.Session() as s:
        q = select(Notification)
        for k, v in where.items():
            q = q.where(getattr(Notification, k) == v)
        return s.scalars(q).all()


def test_review_mode_tells_admin_only(store):
    store.listeners.append(make_listener(store, cfg("review")))
    r = store.ingest(full_upload(), "upload")
    assert r.snapshot_version == 1
    admin = notices(store, audience="admin")
    assert len(admin) == 1 and admin[0].subject == "Rikz: report v1 created – review and send"
    assert notices(store, audience="shareholder") == []


def test_auto_mode_queues_shareholders(store):
    store.listeners.append(make_listener(store, cfg("auto")))
    store.ingest(full_upload(), "upload")
    assert sorted(n.recipient for n in notices(store, audience="shareholder")) == ["a@example.com", "b@example.com"]


def test_rejections_reach_admin_with_reasons(store):
    store.listeners.append(make_listener(store, cfg()))
    store.ingest([("x.xlsx", (MANAFA / "2026-09-24_portfolio.xlsx").read_bytes())], "drive")
    mailer = FakeMailer()
    run_jobs(store, {"mailer": mailer})
    assert mailer.sent[0]["subject"] == "Rikz: an upload was rejected"
    assert "no date of its own" in mailer.sent[0]["text"]


def test_shareholder_email_content_and_sending(store):
    store.ingest(full_upload(), "upload")
    assert queue_shareholder_report(store, cfg(), 1) == 2
    assert queue_shareholder_report(store, cfg(), 1) == 0  # never twice by accident
    mailer = FakeMailer()
    assert run_jobs(store, {"mailer": mailer}) == (2, 0)
    msg = mailer.sent[0]
    assert msg["subject"] == "Rikz portfolio report – as of 24 Sep 2026"
    assert VIEW_LINK in msg["text"] and VIEW_LINK in msg["html"]
    assert "244,711" not in msg["text"] + msg["html"]  # no figures outside the access-controlled site
    assert "access key" in msg["text"] and "not sent by e-mail" in msg["text"]
    assert all(n.status == "sent" and n.sent_at for n in notices(store, audience="shareholder"))
    assert queue_shareholder_report(store, cfg(), 1, resend=True) == 2


def test_unconfigured_mail_retries_then_fails(store):
    store.ingest(full_upload(), "upload")
    queue_shareholder_report(store, cfg(), 1)
    assert run_jobs(store, {}) == (0, 2)
    with store.Session() as s:
        for j in s.scalars(select(Job)):
            j.attempts, j.status = 8, "failed"
        s.commit()
    failed = mark_failed_notifications(store)
    assert len(failed) == 2 and all(n.status == "failed" for n in notices(store, audience="shareholder"))


def test_smtp_session(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            calls.append(("quit",))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, pw):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))
            calls.append(("raw", message_from_bytes(msg.as_bytes())))

    monkeypatch.setattr(mailmod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "reports@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    config = mailmod.MailConfig.from_env()
    assert (config.port, config.security, config.sender) == (587, "starttls", "reports@example.com")
    mailmod.SMTPMailer(config).send("a@example.com", "Hello", "text body", "<p>html</p>")
    assert [c[0] for c in calls] == ["connect", "starttls", "login", "send", "raw", "quit"]
    raw = calls[4][1]
    assert raw.is_multipart() and raw["From"].startswith("Rikz Portfolio Reporting")


def test_mail_off_without_host(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert mailmod.MailConfig.from_env() is None
