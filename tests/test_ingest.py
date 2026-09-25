from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import AWAED, MANAFA
from rikz.ingest.drive import FOLDER, SHEET, XLSX, RemoteFile, poll, walk
from rikz.ingest.worker import enqueue, handler, run_jobs, run_once
from rikz.store.db import Batch, DriveFile, Job, Snapshot, init_db, make_engine
from rikz.store.files import FileStore
from rikz.store.service import Store

NOW = datetime.now(timezone.utc)


class FakeDrive:
    def __init__(self):
        self.folders: dict[str, list[RemoteFile]] = {"root": []}
        self.data: dict[str, bytes] = {}
        self.fail: set[str] = set()
        self.downloads = 0

    def add(self, path, *, folder="root", hours_ago=1, mime=None, name=None):
        name = name or path.name
        mime = mime or ("application/pdf" if name.endswith(".pdf") else XLSX)
        f = RemoteFile(f"id-{name}", name, mime, f"md5-{name}", NOW - timedelta(hours=hours_ago))
        self.folders.setdefault(folder, []).append(f)
        self.data[f.id] = path.read_bytes() if hasattr(path, "read_bytes") else path
        return f

    def list_folder(self, folder_id):
        return list(self.folders.get(folder_id, []))

    def download(self, f):
        self.downloads += 1
        if f.id in self.fail:
            raise ConnectionError("simulated")
        return self.data[f.id]


@pytest.fixture
def store(tmp_path):
    return Store(init_db(make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")), FileStore(tmp_path / "files"))


def snapshots(store):
    with store.Session() as s:
        return s.scalars(select(Snapshot).order_by(Snapshot.version)).all()


def statuses(store):
    with store.Session() as s:
        return {d.name: d.status for d in s.scalars(select(DriveFile))}


def test_walk_follows_subfolders_and_sheets():
    d = FakeDrive()
    d.folders["root"].append(RemoteFile("sub", "Awaed", FOLDER, None, NOW))
    d.add(AWAED / "01-murabaha_confirmation.pdf", folder="sub")
    d.add(MANAFA / "2026-09-24_portfolio.xlsx", mime=SHEET, name="native sheet")
    d.folders["root"].append(RemoteFile("x", "notes.docx", "application/msword", None, NOW))
    assert sorted(f.name for f in walk(d, ["root"])) == ["01-murabaha_confirmation.pdf", "native sheet"]


def test_first_poll_builds_the_report(store):
    d = FakeDrive()
    for p in sorted(AWAED.glob("*.pdf")):
        d.add(p, hours_ago=5)
    d.add(MANAFA / "2026-09-24_portfolio.xlsx", hours_ago=2)
    d.add(MANAFA / "2026-09-24_account_statement.xlsx", hours_ago=2)
    r = poll(store, d, ["root"])
    assert (r.found, r.new, r.waiting, r.errors) == (22, 22, [], [])
    assert [st for _, st, _ in r.batches] == ["accepted", "accepted"]  # all PDFs, then the Manafa pair
    assert len(snapshots(store)) == 1 and snapshots(store)[0].as_of.isoformat() == "2026-09-24"
    before = d.downloads
    r = poll(store, d, ["root"])
    assert r.new == 0 and r.batches == [] and d.downloads == before  # nothing fetched twice


def test_export_waits_for_its_statement(store):
    d = FakeDrive()
    for p in sorted(AWAED.glob("*.pdf")):
        d.add(p, hours_ago=5)
    d.add(MANAFA / "2026-09-24_portfolio.xlsx", hours_ago=1)
    r = poll(store, d, ["root"])
    assert r.waiting == ["2026-09-24_portfolio.xlsx"] and statuses(store)["2026-09-24_portfolio.xlsx"] == "waiting"
    assert snapshots(store) == []
    d.add(MANAFA / "2026-09-24_account_statement.xlsx", hours_ago=0)
    poll(store, d, ["root"])
    assert statuses(store)["2026-09-24_portfolio.xlsx"] == "ingested"
    assert len(snapshots(store)) == 1


def test_export_pairs_with_the_right_statement(store):
    d = FakeDrive()
    for p in sorted(AWAED.glob("*.pdf")):
        d.add(p, hours_ago=5)
    # The 6 Sep statement is closer in time, but it cannot be the 24 Sep export's pair.
    d.add(MANAFA / "2026-09-24_portfolio.xlsx", hours_ago=10)
    d.add(MANAFA / "2026-09-06_account_statement.xlsx", hours_ago=10)
    d.add(MANAFA / "2026-09-24_account_statement.xlsx", hours_ago=12)
    poll(store, d, ["root"])
    with store.Session() as s:
        rejected = s.scalars(select(Batch).where(Batch.status == "rejected")).all()
    assert rejected == []  # the wrong pairing was tried without being recorded
    snap = snapshots(store)[-1]
    assert snap.coverage["manafa"]["statement_to"] == "2026-09-24"
    assert statuses(store)["2026-09-06_account_statement.xlsx"] == "waiting"


def test_stale_export_is_rejected_with_reason(store):
    d = FakeDrive()
    d.add(MANAFA / "2026-09-24_portfolio.xlsx", hours_ago=72)
    poll(store, d, ["root"], wait_hours=48)
    assert statuses(store)["2026-09-24_portfolio.xlsx"] == "rejected"
    with store.Session() as s:
        b = s.scalars(select(Batch)).one()
    assert b.status == "rejected" and "no date of its own" in b.reasons[0]


def test_bad_pdf_does_not_block_good_ones(store):
    d = FakeDrive()
    d.add(AWAED / "01-murabaha_confirmation.pdf")
    d.add(AWAED / "02-murabaha_confirmation.pdf")
    d.add(b"%PDF-1.4 not really a pdf", name="broken.pdf")
    poll(store, d, ["root"])
    st = statuses(store)
    assert st["01-murabaha_confirmation.pdf"] == st["02-murabaha_confirmation.pdf"] == "ingested"
    assert st["broken.pdf"] == "rejected"


def test_download_failure_is_retried(store):
    d = FakeDrive()
    f = d.add(AWAED / "01-murabaha_confirmation.pdf")
    d.fail.add(f.id)
    r = poll(store, d, ["root"])
    assert r.errors and "01-murabaha_confirmation.pdf" not in statuses(store)
    d.fail.clear()
    poll(store, d, ["root"])
    assert statuses(store)["01-murabaha_confirmation.pdf"] == "ingested"


def test_jobs_retry_with_backoff(store):
    calls = []

    @handler("test.flaky")
    def flaky(store_, payload, context):
        calls.append(payload["n"])
        if len(calls) == 1:
            raise RuntimeError("smtp down")

    enqueue(store.Session, "test.flaky", {"n": 1})
    assert run_jobs(store, {}) == (0, 1)
    with store.Session() as s:
        job = s.scalars(select(Job)).one()
        assert job.status == "pending" and job.attempts == 1 and "smtp down" in job.last_error
        job.run_after = NOW - timedelta(minutes=1)  # skip the back-off wait
        s.commit()
    assert run_jobs(store, {}) == (1, 0)


def test_worker_round_survives_drive_errors(store):
    class Broken:
        def list_folder(self, _):
            raise PermissionError("not shared with the service account")

    summary = run_once(store, drive=Broken(), folders=["root"])
    assert "not shared" in summary["drive"]["error"]
