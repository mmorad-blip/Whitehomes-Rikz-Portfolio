import logging

import pytest
from fastapi.testclient import TestClient

from conftest import AWAED
from rikz.store.db import init_db, make_engine
from rikz.store.files import FileStore
from rikz.store.service import Store
from rikz.web.app import create_app
from rikz.web.auth import AccessConfig, hash_key
from rikz.web.security import CLIENT_MAX_FAILURES, redact

VIEW, ADMIN = "V" * 24, "A" * 24


@pytest.fixture
def setup(tmp_path):
    store = Store(init_db(make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")), FileStore(tmp_path / "files"))
    access = AccessConfig(secret=b"s" * 40, tokens={"shareholder": VIEW, "admin": ADMIN},
                          key_hashes={"shareholder": hash_key("GOOD-KEY"), "admin": hash_key("ADMIN-KEY")},
                          public_url="https://report.example", secure_cookies=True)
    return store, access, TestClient(create_app(store, access), base_url="https://testserver")


def test_security_headers(setup):
    _, _, c = setup
    r = c.get(f"/v/{VIEW}/login")
    h = r.headers
    assert "default-src 'none'" in h["content-security-policy"] and "script-src" not in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store" and h["x-robots-tag"].startswith("noindex")
    assert "max-age" in h["strict-transport-security"]
    assert "server" not in h


def test_guessing_is_throttled(setup):
    _, _, c = setup
    for _ in range(CLIENT_MAX_FAILURES):
        assert c.post(f"/v/{VIEW}/login", data={"key": "nope"}).status_code == 401
    r = c.post(f"/v/{VIEW}/login", data={"key": "GOOD-KEY"})
    assert r.status_code == 429 and "Too many wrong keys" in r.text  # even the right key waits


def test_success_clears_failures(setup):
    _, _, c = setup
    for _ in range(CLIENT_MAX_FAILURES - 1):
        c.post(f"/v/{VIEW}/login", data={"key": "nope"})
    assert c.post(f"/v/{VIEW}/login", data={"key": "GOOD-KEY"}, follow_redirects=False).status_code == 303
    assert c.post(f"/v/{VIEW}/login", data={"key": "nope"}).status_code == 401  # counter restarted


def test_new_key_ends_old_sessions(setup):
    _, access, _ = setup
    session = access.make_session("shareholder")
    assert access.read_session(session, "shareholder")
    rotated = AccessConfig(access.secret, access.tokens, {**access.key_hashes, "shareholder": hash_key("NEW-KEY")},
                           access.public_url)
    assert rotated.read_session(session, "shareholder") is None


def test_tokens_never_reach_the_log(setup, caplog):
    _, _, c = setup
    with caplog.at_level(logging.INFO, logger="rikz.web"):
        c.get(f"/v/{VIEW}/login")
        c.get(f"/a/{ADMIN}/")
    text = caplog.text
    assert VIEW not in text and ADMIN not in text and "/v/<token>/login" in text
    assert redact(f"/a/{ADMIN}/admin") == "/a/<token>/admin"


def test_oversized_upload_refused(setup):
    _, _, c = setup
    r = c.post(f"/a/{ADMIN}/upload", content=b"x" * 10, headers={"content-length": str(200 * 1024 * 1024),
                                                                "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_error_pages_are_html(setup):
    _, _, c = setup
    r = c.get("/v/unknown/")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "Not found." in r.text


def test_no_inline_scripts_in_pages(setup):
    store, _, c = setup
    paths = [*sorted(AWAED.glob("*.pdf"))]
    store.ingest([(p.name, p.read_bytes()) for p in paths], "cli")
    c.post(f"/a/{ADMIN}/login", data={"key": "ADMIN-KEY"})
    for page in (c.get(f"/a/{ADMIN}/admin").text, c.get(f"/v/{VIEW}/login").text):
        assert "<script" not in page and "onchange=" not in page and "onclick=" not in page


def test_check_config_flags_problems(monkeypatch, tmp_path, capsys):
    from rikz.cli import main

    for k in ("RIKZ_SECRET_KEY", "RIKZ_VIEW_TOKEN", "RIKZ_ADMIN_TOKEN", "RIKZ_VIEW_KEY_HASH", "RIKZ_ADMIN_KEY_HASH",
              "SMTP_HOST", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RIKZ_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FILE_STORE_DIR", raising=False)
    assert main(["check-config"]) == 1
    out = capsys.readouterr().out
    assert "missing environment variables" in out and "database reachable" in out
    monkeypatch.setenv("RIKZ_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("RIKZ_VIEW_TOKEN", "v" * 32)
    monkeypatch.setenv("RIKZ_ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("RIKZ_VIEW_KEY_HASH", hash_key("x"))
    monkeypatch.setenv("RIKZ_ADMIN_KEY_HASH", hash_key("y"))
    monkeypatch.setenv("RIKZ_PUBLIC_URL", "https://report.example")
    assert main(["check-config"]) == 0
    assert "Ready." in capsys.readouterr().out
