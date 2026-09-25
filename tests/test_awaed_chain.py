from datetime import date
from decimal import Decimal as D

from rikz.awaed.chain import build


def test_wallet_before_last_maturity(awaed, ledger):
    c = build(awaed, ledger, date(2026, 9, 21))
    assert [d.order_id for d in c.outstanding] == ["170816"]
    # 100,000 contributed + returns on deposits 1-19 - 102,324 outstanding
    assert c.wallet == D("100000") + D("2324.27703525") - D("102324")


def test_matured_without_rollover_is_flagged(awaed, ledger):
    c = build(awaed, ledger, date(2026, 9, 24))
    assert c.outstanding == []
    assert c.wallet == D("102406.84636381")
    assert [g.kind for g in c.gaps][-1] == "uninvested"


def test_gaps(awaed, ledger):
    c = build(awaed, ledger, date(2026, 9, 24))
    kinds = [g.kind for g in c.gaps]
    assert "shortfall" not in kinds
    assert kinds.count("rounding") == 13
    assert max(g.amount for g in c.gaps if g.kind == "rounding") < 1
    idle = {(g.date, g.detail.split("(")[1]) for g in c.gaps if g.kind == "idle"}
    assert (date(2026, 3, 11), "9 days later)") in idle


def test_missing_confirmation_shows_as_shortfall(awaed, ledger):
    without_16 = [d for d in awaed if d.order_id != "157927"]
    c = build(without_16, ledger, date(2026, 9, 24))
    short = [g for g in c.gaps if g.kind == "shortfall"]
    assert short and short[0].date == date(2026, 8, 12)


def test_matured_deposit_is_wallet_cash_until_rolled(awaed, ledger):
    c = build(awaed, ledger, date(2026, 9, 24))
    last = c.gaps[-1]
    assert last.kind == "uninvested" and last.date == date(2026, 9, 22)
    assert "102,406.57 SAR held as wallet cash" in last.detail
    assert [d.order_id for d in c.matured][-1] == "170816"
