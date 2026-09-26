import hashlib
import json

import httpx
import pytest

from conftest import AWAED, MANAFA
from rikz.store.db import init_db, is_supabase, make_engine, normalize_url
from rikz.store.files import IntegrityError
from rikz.store.service import Store
from rikz.store.supabase_files import StorageError, SupabaseFileStore

KEY = "service-role-test-key"
BASE = "https://abcd.supabase.co"


class FakeStorage:
    """Just enough of the Supabase Storage REST API, answering the way the
    real service does (missing and duplicate come back as HTTP 400 with a
    404/409 code in the body)."""

    def __init__(self, public=False):
        self.buckets: dict[str, dict] = {}
        self.objects: dict[str, bytes] = {}
        self.public = public
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}" or request.headers.get("apikey") != KEY:
            return httpx.Response(401, json={"statusCode": "401", "error": "Unauthorized"})
        path = request.url.path.removeprefix("/storage/v1")
        if path.startswith("/bucket"):
            if request.method == "POST":
                body = json.loads(request.content)
                self.buckets[body["id"]] = {"id": body["id"], "public": body["public"] or self.public}
                return httpx.Response(200, json={"name": body["id"]})
            bid = path.split("/")[2]
            if bid not in self.buckets:
                return httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found"})
            return httpx.Response(200, json=self.buckets[bid])
        if path.startswith("/object/list/"):
            prefix = json.loads(request.content)["prefix"].strip("/")
            depth = 0 if not prefix else prefix.count("/") + 1
            names = {}
            for key in self.objects:
                parts = key.split("/", 1)[1].split("/")
                if prefix and not "/".join(parts).startswith(prefix + "/"):
                    continue
                child = parts[depth]
                names[child] = None if depth < len(parts) - 1 else f"id-{child[:6]}"
            return httpx.Response(200, json=[{"name": n, "id": i} for n, i in sorted(names.items())])
        key = path.removeprefix("/object/")
        if request.method == "POST":
            if key in self.objects:
                return httpx.Response(400, json={"statusCode": "409", "error": "Duplicate",
                                                 "message": "The resource already exists"})
            self.objects[key] = request.content
            return httpx.Response(200, json={"Key": key})
        if key not in self.objects:
            return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
        return httpx.Response(200, content=b"" if request.method == "HEAD" else self.objects[key])


def fake_store(fake=None):
    fake = fake or FakeStorage()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return SupabaseFileStore(BASE, KEY, "statements", client=client), fake


def test_connection_strings_as_supabase_prints_them():
    pooled = "postgresql://postgres.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    url, args = normalize_url(pooled)
    assert url.startswith("postgresql+psycopg://") and url.endswith("sslmode=require") and args == {}
    url, args = normalize_url(pooled.replace(":5432", ":6543"))
    assert args == {"prepare_threshold": None}  # transaction pooler: no prepared statements
    assert normalize_url("postgres://u:p@db.abcd.supabase.co:5432/postgres")[0].startswith("postgresql+psycopg://")
    assert is_supabase(pooled) and not is_supabase("sqlite:///x.db")
    assert normalize_url("sqlite:///data/rikz.db") == ("sqlite:///data/rikz.db", {})


def test_env_defaults_to_private_schema_on_supabase(monkeypatch, tmp_path):
    from rikz.env import Env

    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres")
    monkeypatch.delenv("DATABASE_SCHEMA", raising=False)
    assert Env.load().database_schema == "rikz"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    assert Env.load().database_schema is None
    monkeypatch.setenv("FILE_STORE", "s3")
    with pytest.raises(RuntimeError):
        Env.load()


def test_bucket_is_created_private():
    store, fake = fake_store()
    assert fake.buckets["statements"]["public"] is False


def test_public_bucket_is_refused():
    fake = FakeStorage(public=True)
    fake.buckets["statements"] = {"id": "statements", "public": True}
    with pytest.raises(StorageError, match="public"):
        fake_store(fake)


def test_write_once_read_verified():
    store, fake = fake_store()
    data = b"%PDF-1.4 statement"
    sha = store.put(data)
    assert sha == hashlib.sha256(data).hexdigest()
    assert f"statements/{sha[:2]}/{sha[2:4]}/{sha}" in fake.objects
    assert store.put(data) == sha  # second upload is a no-op, not an error
    assert store.get(sha) == data and store.exists(sha)
    assert not store.exists("0" * 64)
    with pytest.raises(FileNotFoundError):
        store.get("0" * 64)
    fake.objects[f"statements/{sha[:2]}/{sha[2:4]}/{sha}"] = b"tampered"
    with pytest.raises(IntegrityError):
        store.get(sha)
    other = store.put(b"another file")
    assert sorted(store.all_hashes()) == sorted([sha, other])
    assert store.verify_all() == [sha]


def test_errors_are_reported_not_hidden():
    store, fake = fake_store()
    store.headers = {"Authorization": "Bearer wrong", "apikey": "wrong"}
    with pytest.raises(StorageError, match="HTTP 401"):
        store.put(b"x")


def test_full_ingest_on_supabase_storage(tmp_path):
    store_files, fake = fake_store()
    store = Store(init_db(make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")), store_files)
    paths = [*sorted(AWAED.glob("*.pdf")), MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]
    r = store.ingest([(p.name, p.read_bytes()) for p in paths], "upload")
    assert r.status == "accepted" and r.snapshot_version == 1
    assert len(fake.objects) == 22
    assert store.recalculate().status == "unchanged"  # rebuilt entirely from Supabase storage


def test_export_and_import_store(monkeypatch, tmp_path, capsys):
    from rikz.cli import main

    monkeypatch.setenv("RIKZ_DATA_DIR", str(tmp_path / "a"))
    for k in ("DATABASE_URL", "FILE_STORE_DIR", "FILE_STORE", "DATABASE_SCHEMA"):
        monkeypatch.delenv(k, raising=False)
    files = [str(p) for p in sorted(AWAED.glob("*.pdf"))[:3]]
    assert main(["ingest", *files]) == 0
    assert main(["export-store", str(tmp_path / "backup")]) == 0
    assert len(list((tmp_path / "backup").iterdir())) == 3
    monkeypatch.setenv("RIKZ_DATA_DIR", str(tmp_path / "b"))
    (tmp_path / "backup" / ("f" * 64)).write_bytes(b"damaged")
    assert main(["import-store", str(tmp_path / "backup")]) == 1  # the damaged file is refused
    assert "imported 3 files, skipped 1 damaged" in capsys.readouterr().out


def test_supabase_migration_matches_the_models():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("mig", root / "scripts" / "make_supabase_migration.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    committed = (root / "supabase" / "migrations" / "20260926000000_rikz_schema.sql").read_text()
    assert mig.migration_sql() == committed, "tables changed: add a new migration and bump SCHEMA_VERSION"
    assert committed.count("enable row level security") == committed.count("CREATE TABLE")
    assert "'statements', 'statements', false" in committed
