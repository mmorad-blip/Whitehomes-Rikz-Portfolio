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
from .config import load_ledger, load_mandate, load_settings
from .engine.render import load_excel_reference, to_json, to_text
from .engine.run import report_from_batch
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
    rp = sub.add_parser("report", help="compute the portfolio report from a full upload batch")
    rp.add_argument("files", nargs="+", type=Path)
    rp.add_argument("--json", action="store_true", help="print the report as JSON")
    rp.add_argument("--no-excel", action="store_true", help="do not show Excel V2.2 values alongside")
    ip = sub.add_parser("ingest", help="store an upload batch and update the snapshot series")
    ip.add_argument("files", nargs="+", type=Path)
    sub.add_parser("recalc", help="rebuild the report from stored statements (after a settings change)")
    sub.add_parser("snapshots", help="list stored report versions")
    sub.add_parser("verify-store", help="re-hash every stored file")
    mk = sub.add_parser("make-keys", help="generate the two private links' tokens and access keys")
    mk.add_argument("--role", choices=["shareholder", "admin", "both"], default="both",
                    help="rotate one role's link and key, or create both")
    sv = sub.add_parser("serve", help="run the website")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    rp.add_argument("--mandate", type=Path)
    rp.add_argument("--ledger", type=Path)
    rp.add_argument("--settings", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "make-keys":
        return _make_keys(args.role)
    if args.cmd == "serve":
        import uvicorn

        from .env import open_store
        from .web.app import create_app
        from .web.auth import AccessConfig

        uvicorn.run(create_app(open_store(), AccessConfig.from_env()), host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips="*")
        return 0
    if args.cmd in ("ingest", "recalc", "snapshots", "verify-store"):
        return _store_cmd(args)
    try:
        rule = load_mandate(args.mandate)
        ledger = load_ledger(args.ledger)
        files = [(f.name, f.read_bytes()) for f in args.files]
        batch = parse_batch(files, rule=rule, ledger=ledger, as_of=getattr(args, "as_of", None))
        if args.cmd == "report":
            settings = load_settings(args.settings)
            report = report_from_batch(batch, rule=rule, ledger=ledger, settings=settings)
            excel = {} if args.no_excel else load_excel_reference()
            if excel and report.as_of.isoformat() != "2026-09-24":
                excel = {}  # the reference values are for one date only
            print(to_json(report, settings, excel) if args.json else to_text(report, settings, excel))
            return 0
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


def _make_keys(role: str) -> int:
    import secrets

    from .web.auth import hash_key, new_key, new_token

    roles = ["shareholder", "admin"] if role == "both" else [role]
    env = {"shareholder": ("RIKZ_VIEW_TOKEN", "RIKZ_VIEW_KEY_HASH"), "admin": ("RIKZ_ADMIN_TOKEN", "RIKZ_ADMIN_KEY_HASH")}
    print("# Put these in the server's environment (secret store). They are not saved anywhere else.")
    if role == "both":
        print(f"RIKZ_SECRET_KEY={secrets.token_urlsafe(48)}")
    keys = {}
    for r in roles:
        token, key = new_token(), new_key()
        keys[r] = (token, key)
        print(f"{env[r][0]}={token}")
        print(f"{env[r][1]}={hash_key(key)}")
    print()
    print("# Hand these out; they are shown only now. Links need RIKZ_PUBLIC_URL in front.")
    for r, (token, key) in keys.items():
        area = "v" if r == "shareholder" else "a"
        print(f"{r:<12} link: <RIKZ_PUBLIC_URL>/{area}/{token}/   access key: {key}")
    return 0


def _store_cmd(args) -> int:
    from sqlalchemy import select

    from .env import open_store
    from .store.db import Snapshot

    store = open_store()
    if args.cmd == "verify-store":
        bad = store.files.verify_all()
        print("all stored files match their hashes" if not bad else f"CORRUPT: {', '.join(b[:12] for b in bad)}")
        return 0 if not bad else 1
    if args.cmd == "snapshots":
        with store.Session() as s:
            for snap in s.scalars(select(Snapshot).order_by(Snapshot.version)):
                cov = snap.coverage
                print(f"v{snap.version}  as of {snap.as_of}  created {snap.created_at:%Y-%m-%d %H:%M}  "
                      f"Manafa statement to {cov['manafa']['statement_to']}, "
                      f"{cov['awaed']['confirmations']} Awaed confirmations  NAV {Decimal(snap.headline['total.nav']):,.2f}")
        return 0
    if args.cmd == "recalc":
        res = store.recalculate()
    else:
        res = store.ingest([(f.name, f.read_bytes()) for f in args.files], source="cli")
    print(f"batch {res.batch_id}: {res.status}" + (f", report version {res.snapshot_version}" if res.snapshot_version else ""))
    for r in res.reasons:
        print(f"  rejected: {r}")
    for n in res.notes:
        print(f"  note: {n}")
    for c in res.changes:
        print(f"  change: {c['text']}")
    return 2 if res.status == "rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
