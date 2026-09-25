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
    ex = sub.add_parser("export-store", help="copy every stored statement file into a folder (for backups)")
    ex.add_argument("folder", type=Path)
    im = sub.add_parser("import-store", help="load statement files from a folder made by export-store")
    im.add_argument("folder", type=Path)
    mk = sub.add_parser("make-keys", help="generate the two private links' tokens and access keys")
    mk.add_argument("--role", choices=["shareholder", "admin", "both"], default="both",
                    help="rotate one role's link and key, or create both")
    cc = sub.add_parser("check-config", help="check the environment before going live")
    cc.add_argument("--smtp", action="store_true", help="also log in to the SMTP server (sends nothing)")
    cc.add_argument("--drive", action="store_true", help="also list the Drive folders")
    wk = sub.add_parser("worker", help="poll Google Drive and run background jobs")
    wk.add_argument("--once", action="store_true", help="one round, then exit (for cron)")
    wk.add_argument("--interval", type=int, default=600, help="seconds between rounds (default 600)")
    sv = sub.add_parser("serve", help="run the website")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    rp.add_argument("--mandate", type=Path)
    rp.add_argument("--ledger", type=Path)
    rp.add_argument("--settings", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "make-keys":
        return _make_keys(args.role)
    if args.cmd == "check-config":
        return _check_config(args)
    if args.cmd == "serve":
        import os

        import uvicorn

        from .env import open_store
        from .web.app import create_app
        from .web.auth import AccessConfig

        store = open_store()
        notices = _attach_notifier(store)
        import logging

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        # uvicorn's own access log would print the private link tokens; the
        # app logs every request itself with the token redacted.
        uvicorn.run(create_app(store, AccessConfig.from_env(), notices), host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("RIKZ_TRUSTED_PROXIES", "127.0.0.1"),
                    access_log=False, server_header=False)
        return 0
    if args.cmd in ("ingest", "recalc", "snapshots", "verify-store", "export-store", "import-store"):
        return _store_cmd(args)
    if args.cmd == "worker":
        return _worker(args)
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


def _check_config(args) -> int:
    import json
    import os

    from .config import load_ledger, load_mandate, load_settings
    from .env import Env, open_store
    from .notify.mail import MailConfig
    from .web.auth import AccessConfig

    problems, lines = [], []

    def ok(msg):
        lines.append(f"  ok    {msg}")

    def bad(msg):
        problems.append(msg)
        lines.append(f"  FAIL  {msg}")

    try:
        access = AccessConfig.from_env()
        for role in ("shareholder", "admin"):
            if not access.key_hashes[role].startswith("scrypt$"):
                bad(f"{role} key hash is not an scrypt hash (run rikz make-keys)")
        if not access.public_url.startswith("https://"):
            bad(f"RIKZ_PUBLIC_URL should be https:// (is {access.public_url})")
        if not access.secure_cookies:
            bad("RIKZ_INSECURE_COOKIES is set; only use it for local testing")
        ok("links, keys and secret are set")
    except RuntimeError as exc:
        bad(str(exc))
    env = Env.load()
    try:
        store = open_store(env)
        with store.Session() as s:
            s.execute(__import__("sqlalchemy").text("select 1"))
        ok(f"database reachable ({env.database_url.split('@')[-1]})")
        probe = store.files.put(b"rikz check-config probe")
        where = (f"Supabase bucket {env.supabase_bucket!r}, private" if env.file_store == "supabase"
                 else str(env.file_store_dir))
        ok(f"file store writable ({where}); probe {probe[:8]}")
        if env.database_url.startswith("sqlite"):
            lines.append("  note  SQLite in use; PostgreSQL is recommended for the live site")
        else:
            from sqlalchemy import text as _text

            from .store.db import is_supabase

            schema = env.database_schema or "public"
            with store.Session() as s:
                rows = s.execute(_text(
                    "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = :schema and c.relkind = 'r'"), {"schema": schema}).all()
            if rows and all(r[1] for r in rows):
                ok(f"{len(rows)} tables in schema {schema!r}, row-level security on")
            else:
                bad(f"row-level security is off on: {', '.join(r[0] for r in rows if not r[1]) or 'no tables found'}")
            if is_supabase(env.database_url):
                if schema == "public":
                    bad("on Supabase the tables must not be in the public schema (its web API exposes it); "
                        "unset DATABASE_SCHEMA or set it to rikz")
                if env.file_store != "supabase":
                    lines.append("  note  statement files are on this server's disk; FILE_STORE=supabase keeps "
                                 "them in Supabase Storage instead")
    except Exception as exc:  # noqa: BLE001
        bad(f"storage: {type(exc).__name__}: {exc}")
    try:
        load_mandate(env.config_dir / "mandate.toml")
        load_ledger(env.config_dir / "capital_ledger.toml")
        st = load_settings(env.config_dir / "settings.toml")
        ok("config files load")
        if st.benchmark_is_placeholder:
            lines.append("  note  the SAIBOR benchmark is still a placeholder (config/settings.toml)")
    except Exception as exc:  # noqa: BLE001
        bad(f"config: {exc}")
    try:
        mail = MailConfig.from_env()
        if mail is None:
            lines.append("  note  e-mail off (SMTP_HOST not set)")
        else:
            ok(f"SMTP {mail.host}:{mail.port} ({mail.security})")
            if args.smtp:
                import smtplib
                import ssl

                srv = (smtplib.SMTP_SSL(mail.host, mail.port, timeout=20, context=ssl.create_default_context())
                       if mail.security == "ssl" else smtplib.SMTP(mail.host, mail.port, timeout=20))
                with srv:
                    if mail.security == "starttls":
                        srv.starttls(context=ssl.create_default_context())
                    if mail.user:
                        srv.login(mail.user, mail.password or "")
                ok("SMTP login works")
        if not os.environ.get("RIKZ_SHAREHOLDER_EMAILS"):
            lines.append("  note  RIKZ_SHAREHOLDER_EMAILS not set; shareholders will not be e-mailed")
    except Exception as exc:  # noqa: BLE001
        bad(f"e-mail: {type(exc).__name__}: {exc}")
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            info = json.loads(raw)
            ok(f"Drive service account {info.get('client_email', '?')} (share the folders with it)")
            if args.drive:
                from .ingest.drive import GoogleDrive, walk
                from .ingest.worker import drive_folders

                files = walk(GoogleDrive(info), drive_folders())
                ok(f"Drive folders readable: {len(files)} statement files")
        except Exception as exc:  # noqa: BLE001
            bad(f"Drive: {type(exc).__name__}: {exc}")
    else:
        lines.append("  note  Drive watcher off (GOOGLE_SERVICE_ACCOUNT_JSON not set)")
    print("\n".join(lines))
    print("\nReady." if not problems else f"\n{len(problems)} problem(s) to fix.")
    return 0 if not problems else 1


def _worker(args) -> int:
    import logging
    import os

    from .env import open_store
    from .ingest.drive import GoogleDrive
    from .ingest.worker import drive_folders, run_forever, run_once

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .notify.mail import MailConfig, SMTPMailer
    from .notify.notices import mark_failed_notifications, notify_admins

    store = open_store()
    notices = _attach_notifier(store)
    mail = MailConfig.from_env()
    context = {"mailer": SMTPMailer(mail) if mail else None}
    if mail is None:
        logging.getLogger("rikz.worker").warning("e-mail off (SMTP_HOST not set); notices stay queued")

    def after_round(summary: dict) -> None:
        if notices is None:
            return
        failed = mark_failed_notifications(store)
        if failed:
            notify_admins(store, notices, "job_failed", "Rikz: e-mails could not be sent",
                          "These e-mails failed after repeated attempts: " + "; ".join(failed))
        corrupt = (summary.get("maintenance") or {}).get("corrupt_files")
        if corrupt:
            notify_admins(store, notices, "store_corrupt", "Rikz: stored statement files are damaged",
                          f"{len(corrupt)} stored file(s) no longer match their hash. Restore them from backup: "
                          + ", ".join(c[:12] for c in corrupt))
        err = summary.get("drive", {}).get("error")
        if err:
            from .store.db import get_meta, set_meta

            if get_meta(store.Session, "drive_error_notified") != err:
                set_meta(store.Session, "drive_error_notified", err)
                notify_admins(store, notices, "drive_error", "Rikz: Google Drive could not be read", err)

    drive = GoogleDrive.from_env() if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and drive_folders() else None
    if drive is None:
        logging.getLogger("rikz.worker").info("Drive watcher off (DRIVE_FOLDER_IDS / GOOGLE_SERVICE_ACCOUNT_JSON not set)")
    if args.once:
        summary = run_once(store, drive=drive, context=context)
        after_round(summary)
        print(summary)
        return 0
    run_forever(store, args.interval, drive=drive, context=context, after_round=after_round)
    return 0


def _attach_notifier(store):
    """Register e-mail notices on the store. Returns the notice settings, or
    None when the links are not configured (then nothing is sent)."""
    import logging

    from .notify.notices import NoticeConfig, make_listener
    from .web.auth import AccessConfig

    try:
        access = AccessConfig.from_env()
    except RuntimeError as exc:
        logging.getLogger("rikz").warning("notifications off: %s", exc)
        return None
    cfg = NoticeConfig.from_env(access.link("shareholder"), access.link("admin"))
    store.listeners.append(make_listener(store, cfg))
    return cfg


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
    if args.cmd == "export-store":
        from .store.db import StoredFile

        args.folder.mkdir(parents=True, exist_ok=True)
        with store.Session() as s:
            shas = [f.sha256 for f in s.scalars(select(StoredFile))]
        for sha in shas:
            (args.folder / sha).write_bytes(store.files.get(sha))  # get() checks the hash
        print(f"exported {len(shas)} stored files to {args.folder}")
        return 0
    if args.cmd == "import-store":
        import hashlib as _h

        n = bad = 0
        for p in sorted(args.folder.iterdir()):
            if not p.is_file() or len(p.name) != 64:
                continue
            data = p.read_bytes()
            if _h.sha256(data).hexdigest() != p.name:
                print(f"skipped {p.name[:12]}: content does not match its name")
                bad += 1
                continue
            store.files.put(data)
            n += 1
        print(f"imported {n} files" + (f", skipped {bad} damaged" if bad else ""))
        return 0 if not bad else 1
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
