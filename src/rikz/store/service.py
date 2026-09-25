"""Ingest upload batches into the store and keep the snapshot series current."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..batch import BatchRejected, parse_batch
from ..config import CONFIG_DIR, load_ledger, load_mandate, load_settings
from ..engine.render import load_excel_reference, to_json
from ..engine.run import report_from_batch
from ..errors import Rejected
from ..model import AccountStatement, AwaedDeposit, PortfolioExport
from .db import Batch, Snapshot, StoredFile
from .diff import HEADLINE, changes
from .files import FileStore

CONFIG_FILES = ("mandate.toml", "capital_ledger.toml", "settings.toml")


@dataclass
class IngestResult:
    batch_id: int
    status: str  # accepted | rejected | unchanged
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    snapshot_version: int | None = None
    changes: list[dict] = field(default_factory=list)


class Store:
    def __init__(self, Session, files: FileStore, config_dir: Path = CONFIG_DIR):
        self.Session = Session
        self.files = files
        self.config_dir = config_dir

    # -- configuration -------------------------------------------------------
    def _config(self):
        d = self.config_dir
        return (load_mandate(d / "mandate.toml"), load_ledger(d / "capital_ledger.toml"),
                load_settings(d / "settings.toml"))

    def _config_texts(self) -> dict[str, str]:
        return {n: (self.config_dir / n).read_text("utf8") for n in CONFIG_FILES}

    # -- ingest --------------------------------------------------------------
    def ingest(self, files: list[tuple[str, bytes]], source: str) -> IngestResult:
        rule, ledger, settings = self._config()
        names = [n for n, _ in files]
        hashes = [hashlib.sha256(b).hexdigest() for _, b in files]
        with self.Session() as s:
            try:
                batch = parse_batch(files, rule=rule, ledger=ledger)
            except BatchRejected as exc:
                return self._reject(s, source, names, hashes, [str(r) for r in exc.reasons])

            b = Batch(source=source, status="accepted", file_names=names, file_hashes=hashes)
            s.add(b)
            s.flush()
            notes = [n for n in batch.notes if "were not chained" not in n]
            new_records = []
            records = [*batch.awaed, *(x for x in (batch.export, batch.statement) if x)]
            for rec in records:
                sha = rec.source.sha256
                if s.get(StoredFile, sha) is not None:
                    notes.append(f"{rec.source.name} was already stored; counted once")
                    continue
                new_records.append(rec)
            if not new_records:
                s.rollback()
                return self._record(source, names, hashes, "unchanged", [], notes + ["no new statements in this upload"])

            data = dict(files)
            by_sha = {hashlib.sha256(v).hexdigest(): v for v in data.values()}
            for rec in new_records:
                raw = by_sha[rec.source.sha256]
                self.files.put(raw)
                row = _stored(rec, b.id, batch)
                row.size = len(raw)
                s.add(row)
            s.flush()

            try:
                result = self._recalculate(s, b, rule, ledger, settings)
            except Rejected as exc:
                s.rollback()
                return self._reject(s, source, names, hashes, [str(exc)])
            s.commit()
            result.notes = notes + result.notes
            return result

    def recalculate(self, source: str = "cli") -> IngestResult:
        """Rebuild the report from what is stored (e.g. after a settings change)."""
        rule, ledger, settings = self._config()
        with self.Session() as s:
            b = Batch(source=source, status="accepted", file_names=[], file_hashes=[], reasons=["recalculation"])
            s.add(b)
            s.flush()
            try:
                result = self._recalculate(s, b, rule, ledger, settings)
            except Rejected as exc:
                s.rollback()
                return self._reject(s, source, [], [], [str(exc)])
            s.commit()
            return result

    # -- internals -----------------------------------------------------------
    def _reject(self, s: Session, source, names, hashes, reasons) -> IngestResult:
        return self._record(source, names, hashes, "rejected", reasons, [])

    def _record(self, source, names, hashes, status, reasons, notes) -> IngestResult:
        with self.Session() as s:
            b = Batch(source=source, status=status, file_names=names, file_hashes=hashes, reasons=reasons)
            s.add(b)
            s.commit()
            return IngestResult(b.id, status, reasons, notes)

    def _inputs(self, s: Session) -> tuple[list[StoredFile], list[str]]:
        """All Awaed confirmations plus the latest Manafa statement that has its export."""
        stored = s.scalars(select(StoredFile)).all()
        notes = []
        awaed = [f for f in stored if f.kind == "murabaha_confirmation"]
        exports = {f.paired_with: f for f in stored if f.kind == "portfolio_export" and f.paired_with}
        statements = sorted((f for f in stored if f.kind == "account_statement"),
                            key=lambda f: (f.covers_to, f.stored_at), reverse=True)
        chosen = next((st for st in statements if st.sha256 in exports), None)
        if statements and chosen is not statements[0]:
            notes.append(f"the newest Manafa statement ({statements[0].name}) came without its portfolio export; "
                         "the report uses the latest statement that has one")
        pair = [chosen, exports[chosen.sha256]] if chosen else []
        return awaed + pair, notes

    def _recalculate(self, s: Session, b: Batch, rule, ledger, settings) -> IngestResult:
        inputs, notes = self._inputs(s)
        if not any(f.kind == "account_statement" for f in inputs):
            b.status = "accepted"
            return IngestResult(b.id, "accepted", [], notes + ["stored; the report starts once a Manafa export and "
                                                              "statement of the same day are uploaded"])
        files = [(f.name, self.files.get(f.sha256)) for f in inputs]
        try:
            full = parse_batch(files, rule=rule, ledger=ledger)
        except BatchRejected as exc:
            raise Rejected("; ".join(str(r) for r in exc.reasons)) from None
        report = report_from_batch(full, rule=rule, ledger=ledger, settings=settings)
        future = [d for d in full.awaed if d.order_date > full.as_of]
        if future:
            notes.append(f"{len(future)} Awaed confirmation(s) dated after {full.as_of} are stored and will count "
                         "from the next Manafa statement date")
        excel = load_excel_reference(self.config_dir / "excel_reference.toml") if report.as_of.isoformat() == "2026-09-24" else {}
        doc_text = to_json(report, settings, excel)
        content_hash = hashlib.sha256(doc_text.encode()).hexdigest()

        prev = s.scalars(select(Snapshot).order_by(Snapshot.version.desc()).limit(1)).first()
        if prev is not None and prev.content_hash == content_hash:
            b.status = "unchanged"
            return IngestResult(b.id, "unchanged", [], notes + ["the report is unchanged by this upload"])

        doc = json.loads(doc_text)
        file_names = {f.sha256: f.name for f in inputs}
        prev_doc = json.loads(prev.report) if prev else None
        prev_files = dict(prev.inputs["files"]) if prev else {}
        diff = changes(prev_doc, doc, prev_files, file_names)
        snap = Snapshot(
            version=(prev.version + 1) if prev else 1,
            as_of=report.as_of,
            inputs={"files": file_names, "config": self._config_texts()},
            content_hash=content_hash,
            coverage=_coverage(full),
            headline={k: doc["figures"][k]["value"] for k, _, _ in HEADLINE if k in doc["figures"]},
            changes=diff,
            report=doc_text,
        )
        s.add(snap)
        s.flush()
        b.snapshot_id = snap.id
        return IngestResult(b.id, "accepted", [], notes, snap.version, diff)


def _stored(rec, batch_id: int, batch) -> StoredFile:
    src = rec.source
    common = dict(sha256=src.sha256, name=src.name, channel=src.channel, kind=src.kind, batch_id=batch_id)
    if isinstance(rec, AwaedDeposit):
        return StoredFile(**common, size=0, covers_from=rec.order_date, covers_to=rec.maturity,
                          summary={"order_id": rec.order_id, "principal": str(rec.principal),
                                   "total_return": str(rec.total_return), "product": rec.product})
    if isinstance(rec, AccountStatement):
        return StoredFile(**common, size=0, covers_from=rec.period_start, covers_to=rec.period_end,
                          summary={"closing": str(rec.closing), "rows": [
                              {"row": r.row, "date": r.date.isoformat(), "ref": r.ref, "txn": r.txn.value,
                               "amount": str(r.amount), "balance": str(r.balance)} for r in rec.rows]})
    assert isinstance(rec, PortfolioExport)
    st = batch.statement
    return StoredFile(**common, size=0, covers_from=st.period_start, covers_to=st.period_end,
                      paired_with=st.source.sha256,
                      summary={"positions": [
                          {"sheet": p.sheet, "row": p.row, "oid": p.oid, "principal": str(p.principal),
                           "entry": p.entry.isoformat(), "maturity": p.maturity.isoformat(), "status": p.status.value,
                           "occurrence": p.occurrence} for p in rec.positions]})


def _coverage(full) -> dict:
    st = full.statement
    aw = sorted(full.awaed, key=lambda d: d.ordered_at)
    return {
        "as_of": full.as_of.isoformat(),
        "manafa": {"statement_from": st.period_start.isoformat(), "statement_to": st.period_end.isoformat(),
                   "issued": st.issued_on.isoformat(), "statement": st.source.name, "export": full.export.source.name},
        "awaed": {"confirmations": len(aw), "first_order": aw[0].order_date.isoformat() if aw else None,
                  "last_order": aw[-1].order_date.isoformat() if aw else None},
    }
