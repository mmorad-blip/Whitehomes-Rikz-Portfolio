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
| 3 | Storage and versioned snapshots (Postgres, content-addressed files) | |
| 4 | Dashboard website and PDF report | |
| 5 | Ingestion triggers (upload page, Google Drive watcher) | |
| 6 | Notifications (per-recipient links, email) | |
| 7 | Hardening (access codes, view log, backups, monitoring) | |

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
