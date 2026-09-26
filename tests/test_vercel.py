import builtins

import pytest
from fastapi.testclient import TestClient

from conftest import AWAED, MANAFA
from rikz.store.db import init_db, make_engine
from rikz.store.db_files import DbFileStore
from rikz.store.files import IntegrityError
from rikz.store.service import Store
from rikz.web.app import create_app
from rikz.web.auth import AccessConfig, hash_key

VIEW, ADMIN = "V" * 24, "A" * 24


@pytest.fixture
def db_store(tmp_path):
    Session = init_db(make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}"))
    return Store(Session, DbFileStore(Session))


def test_database_file_store(db_store):
    fs = db_store.files
    sha = fs.put(b"statement bytes")
    assert fs.put(b"statement bytes") == sha and fs.get(sha) == b"statement bytes" and fs.exists(sha)
    assert not fs.exists("0" * 64)
    with pytest.raises(FileNotFoundError):
        fs.get("0" * 64)
    from rikz.store.db import FileBlob

    with db_store.Session() as s:
        s.get(FileBlob, sha).data = b"tampered"
        s.commit()
    with pytest.raises(IntegrityError):
        fs.get(sha)
    assert fs.verify_all() == [sha]


def test_full_ingest_with_files_in_the_database(db_store):
    paths = [*sorted(AWAED.glob("*.pdf")), MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]
    assert db_store.ingest([(p.name, p.read_bytes()) for p in paths], "upload").snapshot_version == 1
    assert db_store.recalculate().status == "unchanged"


def test_plain_keys_from_environment(monkeypatch):
    for k in ("RIKZ_VIEW_KEY_HASH", "RIKZ_ADMIN_KEY_HASH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RIKZ_SECRET_KEY", "s" * 40)
    monkeypatch.setenv("RIKZ_VIEW_TOKEN", "v" * 24)
    monkeypatch.setenv("RIKZ_ADMIN_TOKEN", "a" * 24)
    monkeypatch.setenv("RIKZ_VIEW_KEY", "Shareholders-2026-key")
    monkeypatch.setenv("RIKZ_ADMIN_KEY", "short")
    with pytest.raises(RuntimeError, match="at least 16"):
        AccessConfig.from_env()
    monkeypatch.setenv("RIKZ_ADMIN_KEY", "shareholders-2026-KEY")
    with pytest.raises(RuntimeError, match="must differ"):
        AccessConfig.from_env()
    monkeypatch.setenv("RIKZ_ADMIN_KEY", "Admin-uploads-2026-key")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "rikz-report.vercel.app")
    monkeypatch.delenv("RIKZ_PUBLIC_URL", raising=False)
    acc = AccessConfig.from_env()
    from rikz.web.auth import check_key

    assert check_key("SHAREHOLDERS-2026-KEY", acc.key_hashes["shareholder"])
    assert acc.link("admin") == f"https://rikz-report.vercel.app/a/{'a' * 24}/"


@pytest.fixture
def client(db_store):
    acc = AccessConfig(secret=b"s" * 40, tokens={"shareholder": VIEW, "admin": ADMIN},
                       key_hashes={"shareholder": hash_key("SHARE-KEY-000000"), "admin": hash_key("ADMIN-KEY-000000")},
                       public_url="https://x.example")
    return TestClient(create_app(db_store, acc), base_url="https://testserver")


def test_rate_limit_uses_forwarded_address_on_vercel(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    for i in range(5):
        client.post(f"/v/{VIEW}/login", data={"key": "wrong"}, headers={"x-forwarded-for": "10.0.0.1"})
    assert client.post(f"/v/{VIEW}/login", data={"key": "wrong"}, headers={"x-forwarded-for": "10.0.0.1"}).status_code == 429
    # a different visitor behind the same proxy is not locked out
    assert client.post(f"/v/{VIEW}/login", data={"key": "wrong"}, headers={"x-forwarded-for": "10.0.0.2"}).status_code == 401


def test_cron_endpoint_needs_the_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "c" * 32)
    assert client.get("/cron/worker").status_code == 404
    assert client.get("/cron/worker", headers={"authorization": "Bearer wrong"}).status_code == 404
    r = client.get("/cron/worker", headers={"authorization": f"Bearer {'c' * 32}"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_health_checks_the_database(client):
    assert client.get("/healthz?check=db").json() == {"ok": True, "db": True}


def test_pdf_falls_back_to_print_page_without_weasyprint(client, db_store, monkeypatch):
    paths = [*sorted(AWAED.glob("*.pdf")), MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]
    db_store.ingest([(p.name, p.read_bytes()) for p in paths], "upload")
    real_import = builtins.__import__

    def no_weasyprint(name, *a, **k):
        if name == "weasyprint":
            raise OSError("cannot load library 'libpango-1.0-0'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_weasyprint)
    client.post(f"/v/{VIEW}/login", data={"key": "SHARE-KEY-000000"})
    r = client.get(f"/v/{VIEW}/report.pdf")
    assert r.status_code == 200 and "Save as PDF" in r.text and "244,711.25" in r.text


def test_rejected_upload_leaves_no_file_behind(db_store):
    paths = [*sorted(AWAED.glob("*.pdf")), MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]
    db_store.ingest([(p.name, p.read_bytes()) for p in paths], "upload")
    # the 24 Sep export with the 6 Sep statement parses, but clashes with the stored report
    clash = [MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-06_account_statement.xlsx"]
    import hashlib

    stmt06 = hashlib.sha256((MANAFA / "2026-09-06_account_statement.xlsx").read_bytes()).hexdigest()
    r = db_store.ingest([(p.name, p.read_bytes()) for p in clash], "upload")
    assert r.status == "rejected" and not db_store.files.exists(stmt06)
