from datetime import date
from decimal import Decimal as D

import pypdf
import pytest

from conftest import AWAED
from rikz.errors import Rejected
from rikz.parsers import parse_file

# Values as printed on each confirmation (file number: order ID, order date,
# product, investment amount, total return).
EXPECTED = {
    1: ("119244", date(2026, 1, 7), 7, "75000.00", "59.0625"),
    2: ("120319", date(2026, 1, 11), 7, "25000.00", "19.4445"),
    3: ("123604", date(2026, 1, 26), 7, "25020.00", "18.0003888"),
    4: ("121658", date(2026, 1, 15), 7, "75059.00", "59.83853598"),
    5: ("123620", date(2026, 1, 26), 7, "75119.00", "54.04361336"),
    6: ("125349", date(2026, 2, 2), 7, "100211.00", "72.09580184"),
    7: ("127159", date(2026, 2, 9), 30, "100283.00", "435.67347652"),
    8: ("135175", date(2026, 3, 20), 7, "100719.00", "71.48228868"),
    9: ("138713", date(2026, 4, 5), 7, "100790.00", "75.4524019"),
    10: ("140703", date(2026, 4, 14), 7, "100866.00", "73.54846122"),
    11: ("142723", date(2026, 4, 25), 7, "100939.00", "74.58281771"),
    12: ("144780", date(2026, 5, 5), 30, "101014.00", "448.39003446"),
    13: ("152361", date(2026, 6, 12), 7, "101462.00", "73.98304654"),
    14: ("154530", date(2026, 6, 25), 7, "101536.00", "74.03700512"),
    15: ("155984", date(2026, 7, 2), 7, "101610.00", "74.0909637"),
    16: ("157927", date(2026, 7, 13), 30, "101684.00", "398.26267228"),
    17: ("163937", date(2026, 8, 12), 7, "102082.00", "80.389575"),
    18: ("166073", date(2026, 8, 20), 7, "102162.00", "81.44558964"),
    19: ("168057", date(2026, 8, 31), 7, "102163.00", "80.4533625"),
    20: ("170816", date(2026, 9, 15), 7, "102324.00", "82.56932856"),
}


@pytest.mark.parametrize("n", sorted(EXPECTED))
def test_confirmation(n):
    path = AWAED / f"{n:02d}-murabaha_confirmation.pdf"
    d = parse_file(path.name, path.read_bytes())
    order_id, day, tenor, principal, ret = EXPECTED[n]
    assert (d.order_id, d.order_date, d.tenor_days) == (order_id, day, tenor)
    assert d.principal == D(principal)
    assert d.total_return == D(ret)
    assert d.fees == d.vat == 0
    assert d.total_amount == d.principal
    assert d.source.channel == "awaed" and len(d.source.sha256) == 64


def test_realised_awaed_return_at_full_precision(awaed):
    # Sum of printed returns for deposits 1-19, no cutting to 2 decimals.
    total = sum(d.total_return for d in awaed if d.order_id != "170816")
    assert total == D("2324.27703525")


def test_blank_pdf_rejected(tmp_path):
    w = pypdf.PdfWriter()
    w.add_blank_page(595, 842)
    p = tmp_path / "blank.pdf"
    with open(p, "wb") as fh:
        w.write(fh)
    with pytest.raises(Rejected, match="not a recognised statement"):
        parse_file(p.name, p.read_bytes())


def test_unknown_file_type_rejected():
    with pytest.raises(Rejected, match="unsupported file type"):
        parse_file("notes.txt", b"hello")
