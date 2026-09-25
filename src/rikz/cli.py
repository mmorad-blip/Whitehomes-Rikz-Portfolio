"""rikz parse FILE... : parse an upload batch and print what was found.

Nothing is stored yet (that is phase 3); this shows exactly what an import
would contain, or why it would be rejected."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from .batch import BatchRejected, parse_batch
from .config import load_ledger, load_mandate
from .errors import Rejected
from .manafa.mandate import in_mandate
from .money import fmt


def _print_batch(b, rule) -> None:
    if b.as_of:
        print(f"As of {b.as_of:%d %b %Y}")
    if b.statement:
        s = b.statement
        print(f"\nManafa account statement  sha256 {s.source.sha256[:12]}")
        print(f"  period {s.period_start} to {s.period_end}, issued {s.issued_on}, {len(s.rows)} transactions")
        print(f"  opening {fmt(s.opening)}  deposits {fmt(s.total_deposits)}  withdrawals {fmt(s.total_withdrawals)}  closing {fmt(s.closing)}")
    if b.export:
        e = b.export
        print(f"\nManafa portfolio export  sha256 {e.source.sha256[:12]}")
        print(f"  {len(e.open)} open rows, {len(e.closed)} closed rows")
        print("  statuses: " + ", ".join(f"{k.value} {v}" for k, v in Counter(p.status for p in e.positions).items()))
        mand = [p for p in e.positions if in_mandate(p, rule)]
        print(f"  mandate rule: {rule.describe()}: {len(mand)} rows, {len({p.oid for p in mand})} OIDs")
    if b.matching:
        m = b.matching
        settled = sum(1 for l in m.links if l.settlement)
        print(f"\nRepayment matching: {settled} closed positions linked to their repayments, "
              f"{sum(len(l.partials) for l in m.links)} partial payments, {len(m.unmatched_groups)} unlinked")
        for grp in m.interchangeable:
            print(f"  interchangeable (identical amounts and dates): {', '.join(grp)}")
    if b.reconciliation:
        print("\nMandate reconciliation")
        for line in b.reconciliation.lines():
            print("  " + line)
    if b.awaed:
        print(f"\nAwaed confirmations: {len(b.awaed)}")
        for d in sorted(b.awaed, key=lambda d: d.ordered_at):
            print(f"  order {d.order_id}  {d.order_date}  {d.product:<17} {fmt(d.principal):>12}  return {d.total_return}  matures {d.maturity}")
    if b.chain:
        c = b.chain
        print(f"\nAwaed wallet (derived) on {c.as_of}: {c.wallet:,.2f}; outstanding deposits: "
              f"{', '.join(d.order_id for d in c.outstanding) or 'none'}")
        for g in c.gaps:
            print(f"  [{g.kind}] {g.detail}")
    for n in b.notes:
        print(f"note: {n}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rikz")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="parse an upload batch and show what it contains")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--as-of", type=date.fromisoformat, help="as-of date (defaults to the statement end)")
    p.add_argument("--mandate", type=Path, help="mandate rule file (default config/mandate.toml)")
    p.add_argument("--ledger", type=Path, help="capital ledger file (default config/capital_ledger.toml)")
    args = ap.parse_args(argv)

    try:
        rule = load_mandate(args.mandate)
        ledger = load_ledger(args.ledger)
        files = [(f.name, f.read_bytes()) for f in args.files]
        batch = parse_batch(files, rule=rule, ledger=ledger, as_of=args.as_of)
    except BatchRejected as exc:
        print("Rejected. Nothing from this batch was imported:", file=sys.stderr)
        for r in exc.reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2
    except (Rejected, OSError) as exc:
        print(f"Rejected: {exc}", file=sys.stderr)
        return 2
    _print_batch(batch, rule)
    return 0


if __name__ == "__main__":
    sys.exit(main())
