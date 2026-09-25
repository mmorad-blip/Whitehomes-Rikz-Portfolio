"""Glue: from a parsed batch to a computed report."""

from __future__ import annotations

from ..batch import Batch
from ..config import Contribution, MandateRule, Settings
from ..errors import Rejected
from . import holdings as H
from .metrics import Report, compute


def report_from_batch(batch: Batch, *, rule: MandateRule, ledger: list[Contribution], settings: Settings) -> Report:
    if batch.as_of is None:
        raise Rejected("no as-of date: upload a Manafa statement or pass --as-of")
    if batch.statement is None or batch.matching is None:
        raise Rejected("the report needs the Manafa portfolio export and account statement of the as-of date")
    if batch.chain is None:
        raise Rejected("the report needs the Awaed confirmations")
    if batch.as_of != batch.statement.period_end:
        raise Rejected(
            f"the as-of date {batch.as_of} differs from the Manafa statement's end date "
            f"{batch.statement.period_end}; Manafa figures would be stale"
        )
    hs = H.build(batch.matching, batch.chain, rule, batch.as_of)
    rec = batch.reconciliation
    stmt = batch.statement
    report = compute(
        as_of=batch.as_of,
        holdings=hs,
        ledger=ledger,
        wallets={"awaed": batch.chain.wallet, "manafa": rec.mandate_cash},
        wallet_refs={
            "awaed": ("Awaed wallet timeline from the confirmations and capital ledger",),
            "manafa": (f"Manafa statement {stmt.source.sha256[:8]} closing balance {stmt.closing:,.2f} less "
                       f"{rec.difference:,.2f} outside the mandate",),
        },
        settings=settings,
    )
    report.notes.extend(rec.lines())
    for grp in batch.matching.interchangeable:
        report.notes.append(f"Repayments of {', '.join(grp)} are interchangeable (identical amounts and dates)")
    for g in batch.chain.gaps:
        report.notes.append(f"Awaed [{g.kind}] {g.detail}")
    report.notes.extend(batch.notes)
    return report
