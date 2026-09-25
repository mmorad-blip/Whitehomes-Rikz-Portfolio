import os
from datetime import date

import pytest
from sqlalchemy import select

from conftest import AWAED, MANAFA
from rikz.store.db import Batch, Snapshot, StoredFile, init_db, make_engine
from rikz.store.files import FileStore, IntegrityError
from rikz.store.service import Store

AW = sorted(AWAED.glob("*.pdf"))
PAIR_06 = [MANAFA / "2026-09-06_portfolio.xlsx", MANAFA / "2026-09-06_account_statement.xlsx"]
PAIR_24 = [MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]


def read(paths):
    return [(p.name, p.read_bytes()) for p in paths]


@pytest.fixture
def store(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = make_engine(url)
    if os.environ.get("TEST_DATABASE_URL"):
        from rikz.store.db import Base
        Base.metadata.drop_all(engine)
    return Store(init_db(engine), FileStore(tmp_path / "files"))


def test_file_store_is_content_addressed(tmp_path):
    fs = FileStore(tmp_path)
    sha = fs.put(b"abc")
    assert sha == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert fs.put(b"abc") == sha and fs.get(sha) == b"abc"
    path = tmp_path / sha[:2] / sha[2:4] / sha
    path.chmod(0o640)
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        fs.get(sha)
    assert fs.verify_all() == [sha]


def test_snapshot_series(store):
    r = store.ingest(read(AW[:19]), "upload")
    assert r.status == "accepted" and r.snapshot_version is None  # waiting for a Manafa pair
    r = store.ingest(read(PAIR_06), "upload")
    assert r.snapshot_version == 1
    r = store.ingest(read(AW[19:]), "upload")
    assert r.status == "unchanged"  # dated after the 6 Sep report date
    r = store.ingest(read(PAIR_24), "upload")
    assert r.snapshot_version == 2
    texts = [c["text"] for c in r.changes]
    assert "Report date moved from 2026-09-06 to 2026-09-24" in texts
    assert "Manafa OID-5259-791266: active → closed, paid 2026-09-22, net profit 477.40" in texts
    assert "NAV: 244,424.04 → 244,711.25" in texts
    with store.Session() as s:
        snaps = s.scalars(select(Snapshot).order_by(Snapshot.version)).all()
        assert [x.as_of for x in snaps] == [date(2026, 9, 6), date(2026, 9, 24)]
        assert snaps[1].headline["total.nav"].startswith("244711.25")
        assert snaps[1].coverage["manafa"]["statement_to"] == "2026-09-24"
        assert snaps[1].coverage["awaed"]["confirmations"] == 20
        assert "settings.toml" in snaps[1].inputs["config"]
        assert s.scalars(select(StoredFile)).all().__len__() == 24


def test_rejections_store_nothing(store):
    r = store.ingest(read([PAIR_24[0]]), "upload")
    assert r.status == "rejected" and "no date of its own" in r.reasons[0]
    with store.Session() as s:
        assert s.scalars(select(StoredFile)).all() == []
        assert s.scalars(select(Batch)).one().status == "rejected"


def test_duplicate_upload_is_unchanged(store):
    store.ingest(read(AW + PAIR_24), "upload")
    r = store.ingest(read(AW[:2]), "upload")
    assert r.status == "unchanged"
    assert any("already stored" in n for n in r.notes)


def test_conflict_with_stored_data_rejects_the_upload(store, tmp_path):
    # A statement from an older date whose export clashes: the 24 Sep export
    # with the 6 Sep statement is rejected even though each file parses.
    store.ingest(read(AW + PAIR_24), "upload")
    r = store.ingest(read([PAIR_24[0], PAIR_06[1]]), "upload")
    assert r.status == "rejected" and "after the statement ends" in r.reasons[0]


def test_recalculate_is_idempotent(store):
    store.ingest(read(AW + PAIR_24), "upload")
    assert store.recalculate().status == "unchanged"
