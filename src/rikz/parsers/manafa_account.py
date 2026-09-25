"""Manafa account statement (كشف الحساب). Row dates are DD/MM/YYYY; the period
line and footer are YYYY/MM/DD. The customer block (name, ID, IBAN, address)
is deliberately never read."""

from __future__ import annotations

import re

from .. import dates
from ..errors import Rejected
from ..model import AccountStatement, Source, StatementRow
from ..money import from_cell
from ..vocab import Txn, manafa_txn

TITLE = "كشف الحساب"
SUMMARY = ("الرصيد الافتتاحي", "إجمالي الإيداعات", "إجمالي السحوبات", "إجمالي المبالغ المعلقة", "الرصيد الختامي")
TXN_HEADER = ("التاريخ", "الرقم المرجعي", "نوع العملية", "المبلغ", "الرصيد")
PERIOD = re.compile(r"^فترة البيان: من (\d{4}/\d{2}/\d{2}) إلى (\d{4}/\d{2}/\d{2})$")
ISSUED = re.compile(r"بتاريخ (\d{4}/\d{2}/\d{2})")


def _norm(v):
    return v.strip() if isinstance(v, str) else v


def looks_like(ws) -> bool:
    top = [_norm(c) for row in ws.iter_rows(min_row=1, max_row=1, values_only=True) for c in row]
    return TITLE in top


def _find(rows, want: tuple, what: str) -> int:
    hits = [i for i, r in enumerate(rows) if tuple(_norm(c) for c in r[: len(want)]) == want]
    if len(hits) != 1:
        raise Rejected(f"could not find the {what} heading")
    return hits[0]


def parse(source: Source, ws) -> AccountStatement:
    rows = list(ws.iter_rows(values_only=True))

    periods = [m for r in rows[:4] for c in r if isinstance(c, str) and (m := PERIOD.match(c.strip()))]
    if len(periods) != 1:
        raise Rejected("the statement period line (فترة البيان) is missing, so the period is unknown")
    start = dates.parse(periods[0].group(1), dates.HEADER)
    end = dates.parse(periods[0].group(2), dates.HEADER)
    if end < start:
        raise Rejected("the statement period ends before it starts")

    s = _find(rows, SUMMARY, "account summary (ملخص الحساب)")
    try:
        opening, deposits, withdrawals, pending, closing = (from_cell(v) for v in rows[s + 1][:5])
    except Rejected as exc:
        raise Rejected(f"account summary: {exc.reason}") from None

    t = _find(rows, TXN_HEADER, "transactions (تفاصيل المعاملات)")
    out: list[StatementRow] = []
    footer = None
    for i in range(t + 1, len(rows)):
        r = rows[i]
        if all(v is None for v in r):
            continue
        first = _norm(r[0])
        if isinstance(first, str) and ISSUED.search(first) and all(v is None for v in r[1:]):
            footer = ISSUED.search(first).group(1)
            if any(any(v is not None for v in later) for later in rows[i + 1:]):
                raise Rejected("there are rows after the statement footer")
            break
        excel_row = i + 1
        try:
            d = dates.parse(r[0], dates.STATEMENT)
            txn = manafa_txn(r[1], r[2])
            amount, balance = from_cell(r[3]), from_cell(r[4])
        except Rejected as exc:
            raise Rejected(f"transaction row {excel_row}: {exc.reason}") from None
        if any(v is not None for v in r[5:]):
            raise Rejected(f"transaction row {excel_row} has values beyond the known columns")
        out.append(StatementRow(source, excel_row, d, r[1], _norm(r[2]), txn, amount, balance))

    if footer is None:
        raise Rejected("the statement footer with the issue date is missing; the file may be cut short")
    if not out:
        raise Rejected("the statement has no transactions")

    running = opening
    prev = None
    for row in out:
        running += row.amount
        if running != row.balance:
            raise Rejected(
                f"row {row.row}: running balance {row.balance} does not follow from the previous "
                f"balance and amount (expected {running})"
            )
        if prev and row.date < prev:
            raise Rejected(f"row {row.row}: transactions are not in date order")
        if not start <= row.date <= end:
            raise Rejected(f"row {row.row}: date {row.date} is outside the statement period")
        prev = row.date
    if running != closing:
        raise Rejected(f"closing balance {closing} does not equal the last running balance {running}")
    # The summary's deposits and withdrawals are bank/Apple Pay money in and
    # cash withdrawals out; rewards and re-deposits are not counted in either.
    dep = sum((r.amount for r in out if r.txn is Txn.DEPOSIT), opening * 0)
    wd = -sum((r.amount for r in out if r.txn is Txn.WITHDRAWAL), opening * 0)
    if dep != deposits or wd != withdrawals:
        raise Rejected(
            f"the account summary (deposits {deposits}, withdrawals {withdrawals}) does not agree "
            f"with the transactions (deposits {dep}, withdrawals {wd})"
        )

    return AccountStatement(
        source=source,
        period_start=start,
        period_end=end,
        issued_on=dates.parse(footer, dates.HEADER),
        opening=opening,
        total_deposits=deposits,
        total_withdrawals=withdrawals,
        total_pending=pending,
        closing=closing,
        rows=tuple(out),
    )
