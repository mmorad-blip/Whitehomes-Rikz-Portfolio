from datetime import date
from decimal import Decimal

from rikz.web import periods


def test_rollovers_are_not_new_capital():
    weekly = [{"start": f"2026-07-{d:02d}", "maturity": f"2026-07-{d + 7:02d}", "principal": "100000",
               "state": "closed", "realised_net": "90", "paid_on": None} for d in (1, 8, 15)]
    w = periods.Window(date(2026, 7, 1), date(2026, 7, 31))
    monthly = [{"month": "2026-07-01", "contributed": "20000"}]
    m = periods.with_capital(periods.measure(weekly, date(2026, 10, 26), w), monthly, w)
    assert m["deployed"] == Decimal(300000)
    assert m["injected"] == Decimal(20000)
    assert m["reinvested"] == Decimal(280000)
