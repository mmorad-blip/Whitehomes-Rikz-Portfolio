from datetime import date, datetime
from decimal import Decimal as D

import pytest

from rikz import dates
from rikz.errors import Rejected
from rikz.money import from_cell, parse_pdf_sar, parse_percent, parse_sar
from rikz.vocab import Status, Txn, manafa_status, manafa_tenor_months, manafa_txn


def test_sar_strings():
    assert parse_sar("SAR 15,540.00") == D("15540.00")
    assert parse_sar("SAR 0.00") == D("0.00")
    assert parse_pdf_sar("101,684.00 SAR") == D("101684.00")
    assert parse_pdf_sar("82.56932856 SAR") == D("82.56932856")  # never cut to 2 decimals
    for bad in ("15,540.00", "SAR 1,55,40.00", "SAR abc", 15540.0, None):
        with pytest.raises(Rejected):
            parse_sar(bad)


def test_cells_keep_exact_cents():
    assert from_cell(74725.37) == D("74725.37")
    assert from_cell(-18.07) == D("-18.07")
    with pytest.raises(Rejected):
        from_cell(1.005)
    with pytest.raises(Rejected):
        from_cell("74725.37")


def test_percent():
    assert parse_percent("3.60%") == D("0.036")


def test_each_source_has_its_own_date_format():
    assert dates.parse("01.06.2026", dates.EXPORT) == date(2026, 1, 6)  # MM.DD.YYYY
    assert dates.parse("01/06/2026", dates.STATEMENT) == date(2026, 6, 1)  # DD/MM/YYYY
    assert dates.parse("2026/09/24", dates.HEADER) == date(2026, 9, 24)
    with pytest.raises(Rejected):
        dates.parse("13.01.2026", dates.EXPORT)  # day-first date in the export
    with pytest.raises(Rejected):
        dates.parse("2026-01-06", dates.EXPORT)
    with pytest.raises(Rejected):
        dates.parse("06.01.2026", dates.STATEMENT)


def test_awaed_order_date():
    assert dates.parse_awaed_order("PM t01:26 2026-04-14") == datetime(2026, 4, 14, 13, 26)
    assert dates.parse_awaed_order("AM t12:25 2026-06-25") == datetime(2026, 6, 25, 0, 25)
    with pytest.raises(Rejected):
        dates.parse_awaed_order("14/04/2026")


def test_vocabulary():
    assert manafa_status("متعثرة") is Status.DEFAULTED
    assert manafa_status("متأخرة") is Status.DELAYED
    assert manafa_status("سداد مبكر") is Status.REPAID_EARLY
    assert manafa_tenor_months("3 اشهر ") == 3
    assert manafa_txn("MIPF0000000020616283", "أجرة الوكالة") is Txn.FEE
    assert manafa_txn("MIPF0000000020616283", "استرجاع مبلغ الخصم") is Txn.FEE_REBATE
    assert manafa_txn("MDLP0000000017317439", "مكافأة ") is Txn.REWARD
    with pytest.raises(Rejected, match="unknown position status"):
        manafa_status("ملغاة")
    with pytest.raises(Rejected, match="unknown transaction type"):
        manafa_txn("MWWD0000000018039389", "استثمار")  # known word, wrong reference prefix
