"""Parse an upload batch. A batch is all-or-nothing: if any file is rejected,
or the files do not fit together, nothing in it is imported."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .awaed.chain import Chain, build
from .config import Contribution, MandateRule
from .errors import Rejected
from .manafa.mandate import Reconciliation, reconcile
from .manafa.matching import Matching, match
from .model import AccountStatement, AwaedDeposit, PortfolioExport
from .parsers import parse_file


class BatchRejected(Exception):
    def __init__(self, reasons: list[Rejected]):
        self.reasons = reasons
        super().__init__("; ".join(str(r) for r in reasons))


@dataclass
class Batch:
    awaed: list[AwaedDeposit] = field(default_factory=list)
    export: PortfolioExport | None = None
    statement: AccountStatement | None = None
    matching: Matching | None = None
    reconciliation: Reconciliation | None = None
    chain: Chain | None = None
    as_of: date | None = None
    notes: list[str] = field(default_factory=list)


def parse_batch(
    files: list[tuple[str, bytes]],
    *,
    rule: MandateRule,
    ledger: list[Contribution],
    as_of: date | None = None,
) -> Batch:
    batch = Batch()
    errors: list[Rejected] = []
    seen: dict[str, str] = {}
    exports, statements = [], []
    for name, data in files:
        try:
            rec = parse_file(name, data)
        except Rejected as exc:
            errors.append(exc)
            continue
        if rec.source.sha256 in seen:
            batch.notes.append(f"{name} is the same file as {seen[rec.source.sha256]}; counted once")
            continue
        seen[rec.source.sha256] = name
        if isinstance(rec, AwaedDeposit):
            batch.awaed.append(rec)
        elif isinstance(rec, PortfolioExport):
            exports.append(rec)
        else:
            statements.append(rec)
    if errors:
        raise BatchRejected(errors)

    if len(exports) > 1 or len(statements) > 1:
        raise BatchRejected([Rejected("upload one Manafa portfolio export together with one account statement per batch")])
    if exports and not statements:
        raise BatchRejected([Rejected(
            "the Manafa portfolio export has no date of its own; upload it together with the account "
            "statement downloaded the same day", file=exports[0].source.name)])

    batch.export = exports[0] if exports else None
    batch.statement = statements[0] if statements else None
    if as_of is None and batch.statement:
        as_of = batch.statement.period_end
    batch.as_of = as_of

    try:
        if batch.export and batch.statement:
            batch.matching = match(batch.export, batch.statement)
            batch.reconciliation = reconcile(batch.statement, batch.matching, rule, ledger)
            batch.notes.extend(batch.matching.notes)
            for c in batch.reconciliation.missing_contributions:
                batch.notes.append(
                    f"capital ledger contribution of {c.amount:,.2f} on {c.date} has no matching deposit "
                    "on the Manafa statement"
                )
        if batch.awaed:
            if as_of is None:
                batch.notes.append("Awaed deposits were not chained: no as-of date (no statement and no --as-of)")
            else:
                batch.chain = build(batch.awaed, ledger, as_of)
    except Rejected as exc:
        raise BatchRejected([exc]) from None
    return batch
