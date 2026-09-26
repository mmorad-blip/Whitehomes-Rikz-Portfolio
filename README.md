# Whitehomes × Rikz portfolio reporting

Management reporting for the Whitehomes capital-market investment (250,000 SAR:
Whitehomes 215,000, Rikz 35,000) across two channels, **Awaed Alosool Capital**
(murabaha deposits) and **Manafa** (financing opportunities). Figures come from
the platforms' own statements. Every number traces back to a statement row.

```
upload / Drive watcher → parse → store raw file + rows → recalculate → snapshot → dashboard + PDF → email
          phase 5         phase 1        phase 3            phase 2      phase 3      phase 4        phase 6
```

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Parsers and domain model | done |
| 2 | Calculation engine (XIRR, NAV, yields, credit, ladder, forecast, policy) | done |
| 3 | Storage and versioned snapshots (Postgres, content-addressed files) | done |
| 4 | Dashboard website and PDF report | done |
| 5 | Ingestion triggers (upload page, Google Drive watcher) | done |
| 6 | Notifications (two links, email) | done |
| 7 | Hardening (security, deployment, backups, monitoring) | done |

## Phase 1: what works

```
pip install -e '.[dev]'
rikz parse PORTFOLIO.xlsx STATEMENT.xlsx CONFIRMATION*.pdf
pytest
```

`rikz parse` identifies each file from its content, parses it, and prints what
an import would contain. If anything doesn't fit, it prints why and exits
non-zero.

* **Awaed confirmation (PDF)**: order ID, order date/time, product → contract
  days, investment amount, total return at full printed precision, fees, VAT,
  total amount.
* **Manafa portfolio export (xlsx)**: both sheets. Dates are read as
  `MM.DD.YYYY`, amounts from `SAR x,xxx.xx`, and statuses mapped (active,
  delayed, defaulted, repaid, repaid early).
* **Manafa account statement (xlsx)**: period and issue date. Rows are read as
  `DD/MM/YYYY` with reference prefix and Arabic transaction type mapped
  together. The running balance is checked on every row, and the summary
  totals against the rows. **The customer block (name, ID, IBAN, address) is
  never read.**
* **Repayment matching**: the statement doesn't name the opportunity, so
  repayments are linked to positions by principal, net profit per OID, and
  dates (see `src/rikz/manafa/matching.py`). Ties between identical positions
  are reported as interchangeable.
* **Mandate rule** (`config/mandate.toml`, editable). Includes a reconciliation
  of mandate cash against the account balance.
* **Awaed chaining**: a derived wallet timeline that flags rounding, shortfalls
  (missing confirmations), idle cash, and matured deposits with no rollover.
* **Batches are all-or-nothing.** A rejected file rejects the whole batch with
  a plain-English reason. A portfolio export has no date of its own, so it's
  only accepted together with the account statement from the same day.

## Phase 2: the calculation engine

```
rikz report PORTFOLIO.xlsx STATEMENT.xlsx CONFIRMATION*.pdf          # text report
rikz report ... --json                                              # everything, with traces
```

The engine follows the Excel model's definitions (`src/rikz/engine/metrics.py`)
but takes every input from the statements:

* **Returns by channel:** capital, fees, realised profit, principal by state,
  provision, accrued income, wallet cash, NAV, net gain, XIRR (accounting and
  recovery views), capital-weighted and average deal yield, utilisation and
  idle cash.
* **Other sections:** Manafa credit risk; exposure by rating; allocation vs
  policy and capital by shareholder; realised income by period; liquidity
  ladder; sensitivity to loss on defaults; one-year forecast; policy limit
  checks; monthly table.
* **Tracing:** every figure lists what it's built from (positions, ledger
  lines, or other figures). Every position lists its export row and statement
  rows.
* **Excel comparison:** where a figure differs from the Excel V2.2 value
  (`config/excel_reference.toml`), both are shown. Definitions that differ on
  purpose keep an "Excel method" figure alongside: utilisation and recovered
  share.
* **Checks:** each wallet's statement-based balance is checked against the
  estimate (contributed + realised − outstanding).
* **Settings and policy** live in `config/settings.toml`, and every value is
  printed in the report's assumptions.

## Phase 3: storage and snapshots

```
export DATABASE_URL=postgresql+psycopg://user@host/rikz   # default: sqlite:///data/rikz.db
export FILE_STORE_DIR=/srv/rikz/files                     # default: data/files
rikz ingest FILES...     # store an upload and update the report series
rikz recalc              # rebuild from stored statements after a settings change
rikz snapshots           # list report versions with statement coverage
rikz verify-store        # re-hash every stored file
```

* **Files** are stored once each under their SHA-256, are read-only on disk, and
  are checked against the hash on every read.
* **An upload is all-or-nothing.** It's parsed on its own first. Then the whole
  report is rebuilt from everything stored, inside the same database
  transaction. An upload that conflicts with stored data is rejected with its
  reason, and nothing from it is kept.
* **Each rebuild that changes the report writes a new snapshot.** A snapshot is
  an immutable, numbered version. It holds the full report, the input file
  hashes and config used, the statement coverage (Manafa statement period,
  Awaed confirmations), headline figures, and "changes since last report".
  The changes cover new statements, new or closed positions, status changes,
  every headline figure that moved (old → new), and policy checks that
  changed state.
* **The report date** is the date of the latest Manafa statement that came
  with its portfolio export. Awaed confirmations dated later are stored and
  count from the next statement date.

## Phase 4: the website and PDF

```
rikz make-keys           # prints the two private links' tokens + access keys (once) and the env lines
export RIKZ_SECRET_KEY=... RIKZ_VIEW_TOKEN=... RIKZ_VIEW_KEY_HASH=... RIKZ_ADMIN_TOKEN=... RIKZ_ADMIN_KEY_HASH=...
export RIKZ_PUBLIC_URL=https://report.example.com
rikz serve --host 0.0.0.0 --port 8000
```

* **Two private links, each behind its own access key.**
  `<RIKZ_PUBLIC_URL>/v/<token>/` is for shareholders.
  `<RIKZ_PUBLIC_URL>/a/<token>/` is for the admin (uploads come in phase 5).
  A wrong token gives a plain 404. Keys are stored only as scrypt hashes in
  environment variables. Sessions use a signed, HttpOnly, SameSite=Strict
  cookie scoped to its own link.
* **Pages read only stored snapshots,** never a live calculation:
  * *Dashboard:* changes since the last report, key figures (with the Excel
    value where it differs), NAV composition, realised profit by month,
    returns by channel, policy checks, credit risk, rating exposure,
    allocation and capital by shareholder, liquidity, income by period,
    sensitivity, forecast, monthly table, reconciliation and notes, and
    assumptions.
  * *Positions:* every position with the statement rows behind it.
  * *PDF:* the same template, rendered by WeasyPrint.
* **Period selector:** lists every report version with its as-of date. The
  header shows the statement coverage of the version you're viewing.
* **No external assets:** charts are server-side SVG, and there are no
  scripts. Pages follow the device's light or dark mode and work at phone
  width.
* **View log:** records each page view with role, page, report version and a
  keyed hash of the visitor's address. IP addresses are never stored.

## Phase 5: getting statements in

**Admin upload.** The admin link opens the report, plus an **Admin** page. It has:

* multi-file upload (CSRF-protected, at most 60 files of 25 MB each);
* "Recalculate from stored statements", for after a settings, ledger or
  mandate change;
* recent uploads with their status and reasons;
* report versions, Google Drive status, pending jobs, and the view log.

**Google Drive watcher** (`rikz worker`, every 10 minutes by default; use
`--once` for cron):

```
export DRIVE_FOLDER_IDS=<folder id>,<folder id>        # sub-folders are included
export GOOGLE_SERVICE_ACCOUNT_JSON='{...}'              # read-only; share the folders with its e-mail
export RIKZ_DRIVE_WAIT_HOURS=48                        # optional
```

* **Each Drive file is fetched once,** and native Google Sheets are exported
  as xlsx.
* **Awaed confirmations** found together are ingested together. If they fail
  the checks as a group, each is tried on its own.
* **Manafa pairing:** a portfolio export is paired with the account statement
  uploaded closest in time that passes the cross-checks. A pairing attempt
  that fails is not recorded.
* **Waiting files:** an export whose statement hasn't arrived waits, and is
  rejected with its reason after `RIKZ_DRIVE_WAIT_HOURS`. A statement
  without its export waits the same way.
* **Failures:** a Drive failure (network, permissions) is logged, shown on
  the admin page and retried next round. Background jobs retry with back-off
  (2, 4, 8 … minutes, up to 8 attempts).

## Phase 6: notifications

There are **two links only**: one for shareholders and one for the admin,
each with its own access key (phase 4). Email only ever carries the link.

```
export SMTP_HOST=smtp.example.com SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM=reports@example.com
export SMTP_SECURITY=starttls        # or ssl / none; SMTP_PORT defaults to 587 (465 for ssl)
export RIKZ_SHAREHOLDER_EMAILS=a@example.com,b@example.com
export RIKZ_ADMIN_EMAILS=you@example.com
export RIKZ_SHAREHOLDER_EMAIL_MODE=review   # default; "auto" sends as soon as a report is created
```

* **Shareholders** get one email per report version. It contains the
  shareholder link, the report date and how many things changed. There are no
  figures, no attachment and never the access key, so the report stays behind
  the key even if the email is forwarded.
* **Review first (default):** a new report emails the admin, and nothing goes
  to shareholders until you press **Send to shareholders** on the admin page.
  A version is never sent twice by accident, and **Send again** is explicit.
* **The admin** is emailed about every new report (with the changes list),
  every rejected upload (with the reasons), Google Drive read errors (once per
  distinct error) and emails that failed after all retries.
* **Sending:** each recipient gets their own email, so addresses aren't shared.
  Emails are queued as jobs and sent by `rikz worker`, with retries.

## Phase 7: hardening and deployment

See **[docs/OPERATIONS.md](docs/OPERATIONS.md)** for going live, routine use,
key rotation, backups and restore, monitoring, data retention and the
security summary. Deployment files:

* `docker-compose.yml` runs PostgreSQL, the website, the worker, and Caddy
  (automatic HTTPS). `.env.example` lists every setting.
* `Dockerfile`: non-root image. Dependencies are pinned in
  `requirements.txt`, and the image includes a health check.
* `scripts/backup.sh`: nightly database, file store and config backup, with
  a check that the dump can be read.
* `rikz check-config [--smtp] [--drive]`: checks everything before going
  live.
* `.github/workflows/ci.yml`: tests on Python 3.11 and 3.12, with SQLite and
  PostgreSQL, plus a Docker build and PDF smoke test.

Hardening in the app:

* **Log redaction:** request logs replace link tokens with `<token>`, and the
  server's own access log is off.
* **Guessing limits:** a device waits 15 minutes after 5 wrong keys, and a
  link pauses sign-in after 50 wrong keys in an hour.
* **Key rotation** ends existing sessions.
* **Security headers:** strict CSP (no scripts at all), no framing,
  no-referrer, no-store, HSTS and noindex.
* **Error pages and size limits:** HTML error pages, a 100 MB request cap,
  and upload limits.
* **Daily maintenance:** re-hashes the file store (the admin is emailed if a
  file is damaged) and trims the view log (400 days) and sign-in log
  (30 days).

## Supabase

The live setup is Supabase (project `whitehomes-rikz-portfolio`, Frankfurt).
The database objects are versioned in `supabase/migrations/`, generated from
the app's models by `scripts/make_supabase_migration.py`; a test keeps the two
in step. The site runs on Supabase: Postgres for the database and a private Storage
bucket for the statement files. Set `DATABASE_URL` to the Supabase
connection string, and `FILE_STORE=supabase` with `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY`. Then use `docker-compose.supabase.yml`.

* **Tables** live in their own `rikz` schema, with row-level security on,
  so Supabase's automatic web API can't reach them.
* **The bucket must be private,** and the app refuses a public one.
* `rikz export-store` and `rikz import-store` move statement files between
  the two storage options and into backups.

Setup and security notes are in [docs/OPERATIONS.md](docs/OPERATIONS.md#2b-running-on-supabase-instead).

## Rules this code follows

* Statements are the source of truth. The Excel model was typed by hand and
  is only used to check against.
* No guessing: an unknown date shape, status, transaction word or product
  rejects the file.
* Money is `Decimal`. Awaed returns keep every printed decimal.
* Nothing about a real person or account goes in the repo.
  `tests/fixtures` holds only redacted copies made with
  `scripts/redact_fixture.py`, and `tests/test_privacy.py` fails if an IBAN,
  national ID or client tax number appears anywhere tracked.
* Secrets (from phase 3 on) are read from environment variables only.

## Assumptions (to be printed in every report)

1. Awaed "Week Murabaha" = 7 days and "1 Month Murabaha" = 30 days from the order date.
2. Manafa accrual uses each position's actual entry-to-maturity days.
3. Split investments (same OID) are separate positions. For the default rate by count, one OID counts once.
4. Dedup key for Manafa positions is OID + entry date + principal + occurrence.
   The occurrence number is needed because the platform lists two separate
   1,000 SAR investments in OID-2782-346299 on the same day.
5. Unpaid profit on a delayed note stays expected until paid. No write-off while guarantees stand.
6. Troubled exposure = delayed + defaulted principal before provision ÷ total NAV.
7. An Awaed deposit matures on its maturity date. From then its principal and
   return are realised and held as wallet cash until a later confirmation shows
   them rolled over.
8. The capital ledger (`config/capital_ledger.toml`) is kept by hand until bank statements are ingested.
