from collections import Counter
from datetime import date
from decimal import Decimal as D

import pytest

from conftest import MANAFA, edited
from rikz.errors import Rejected
from rikz.parsers import parse_file
from rikz.vocab import Status, Txn

STMT = MANAFA / "2026-09-24_account_statement.xlsx"
EXPORT = MANAFA / "2026-09-24_portfolio.xlsx"


def test_statement_24_sep(stmt_0924):
    s = stmt_0924
    assert (s.period_start, s.period_end, s.issued_on) == (date(2025, 8, 9), date(2026, 9, 24), date(2026, 9, 24))
    assert (s.opening, s.total_deposits, s.total_withdrawals, s.closing) == (
        D("0.00"), D("179177.50"), D("4454.86"), D("113839.88"))
    assert len(s.rows) == 287
    first, last = s.rows[0], s.rows[-1]
    assert (first.date, first.ref, first.txn, first.amount) == (date(2025, 10, 20), "MDWD0000000016437056", Txn.DEPOSIT, D("1000.00"))
    assert (last.date, last.txn, last.amount, last.balance) == (date(2026, 9, 24), Txn.VAT, D("-5.93"), D("113839.88"))
    counts = Counter(r.txn for r in s.rows)
    assert counts[Txn.INVESTMENT] == 63 and counts[Txn.PRINCIPAL] == 47 and counts[Txn.PROFIT] == 42
    assert counts[Txn.FEE_REBATE] == 2 and counts[Txn.REWARD] == 5 and counts[Txn.REDEPOSIT] == 1


def test_statement_6_sep(stmt_0906):
    assert stmt_0906.period_end == date(2026, 9, 6)
    assert stmt_0906.closing == D("74725.37")
    assert len(stmt_0906.rows) == 263


def test_statement_never_reads_the_customer_block(stmt_0924):
    text = repr(stmt_0924)
    assert "REDACTED" not in text
    assert not hasattr(stmt_0924, "customer")


def test_export_24_sep(export_0924):
    e = export_0924
    assert (len(e.open), len(e.closed)) == (21, 42)
    assert Counter(p.status for p in e.positions) == {
        Status.ACTIVE: 20, Status.DEFAULTED: 1, Status.REPAID: 31, Status.REPAID_EARLY: 11}
    p = e.open[0]
    assert (p.oid, p.tenor_months, p.rate, p.rating, p.principal, p.net_profit, p.total) == (
        "OID-1432-698593", 3, D("0.036"), "B", D("15000.00"), D("415.80"), D("15540.00"))
    assert (p.entry, p.maturity, p.status) == (date(2026, 1, 6), date(2026, 4, 6), Status.DEFAULTED)


def test_export_6_sep(export_0906):
    assert (len(export_0906.open), len(export_0906.closed)) == (25, 37)
    assert Counter(p.status for p in export_0906.open)[Status.DELAYED] == 1


def test_dedup_keys(export_0924):
    keys = [p.key for p in export_0924.positions]
    assert len(set(keys)) == len(keys) == 63
    # Two separate 1,000 investments in the same opportunity on the same day.
    twins = [p for p in export_0924.positions if p.oid == "OID-2782-346299"]
    assert [p.occurrence for p in twins] == [1, 2]
    # A split investment: same OID, different principal, two positions.
    split = [p.principal for p in export_0924.positions if p.oid == "OID-7852-316766"]
    assert split == [D("5000.00"), D("10000.00")]


def test_exports_share_keys_across_dates(export_0906, export_0924):
    # Positions closed by 6 Sep carry the same key in the 24 Sep export.
    closed_then = {p.key for p in export_0906.closed}
    assert closed_then <= {p.key for p in export_0924.closed}


def _cell(sheet, ref, value):
    def edit(wb):
        (wb[sheet] if sheet else wb.worksheets[0])[ref] = value
    return edit


@pytest.mark.parametrize("ref,value,reason", [
    ("A16", "2025-10-20", "not in the expected DD/MM/YYYY format"),
    ("C16", "تحويل", "unknown transaction type"),
    ("D16", 1000.5, "running balance"),
    ("A2", None, "period line"),
    ("E12", 1.0, "closing balance"),
])
def test_statement_rejections(ref, value, reason):
    data = edited(STMT, _cell(None, ref, value))
    with pytest.raises(Rejected, match=reason):
        parse_file("s.xlsx", data)


def test_statement_cut_short_is_rejected():
    def edit(wb):
        ws = wb.worksheets[0]
        ws.delete_rows(ws.max_row)
    with pytest.raises(Rejected, match="footer"):
        parse_file("s.xlsx", edited(STMT, edit))


@pytest.mark.parametrize("ref,value,reason", [
    ("I2", "06/01/2026", "MM.DD.YYYY"),
    ("I2", "13.06.2026", "MM.DD.YYYY"),
    ("K2", "ملغاة", "unknown position status"),
    ("F2", "15000", "SAR 1,000.00"),
    ("K2", "تم السداد", "does not belong on this sheet"),
])
def test_export_rejections(ref, value, reason):
    data = edited(EXPORT, _cell("الاستثمارات القائمة", ref, value))
    with pytest.raises(Rejected, match=reason):
        parse_file("e.xlsx", data)
