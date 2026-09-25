from datetime import date
from decimal import Decimal as D

import pytest

from rikz.errors import Rejected
from rikz.manafa.mandate import in_mandate, reconcile
from rikz.manafa.matching import match


@pytest.fixture(scope="module")
def m24(export_0924, stmt_0924):
    return match(export_0924, stmt_0924)


def _link(m, oid, principal=None):
    return next(l for l in m.links if l.position.oid == oid and (principal is None or l.position.principal == D(principal)))


def test_every_repayment_linked(m24, export_0924):
    assert len(m24.links) == 63
    assert all(l.settlement for l in m24.links if l.position.status.closed)
    assert m24.unmatched_groups == []
    assert sum(len(l.partials) for l in m24.links) == 5


def test_delayed_note_paid_in_instalments(m24):
    l = _link(m24, "OID-5259-791266")
    assert [(g.date, g.principal) for g in l.partials] == [(date(2026, 9, 19), D("7662.86"))]
    assert l.paid_on == date(2026, 9, 22)
    assert l.principal_received == D("15000.00")
    assert l.net_profit_received == D("477.40")


def test_early_repayment_and_fee_detail(m24):
    l = _link(m24, "OID-3858-307228")
    assert l.paid_on == date(2026, 9, 24) and l.net_profit_received == D("152.37")
    e = _link(m24, "OID-2997-991239")
    assert (e.settlement.gross_profit, e.net_profit_received) == (D("355.33"), D("273.62"))


def test_split_oid_nets_add_to_export_total(m24):
    a, b = _link(m24, "OID-7852-316766", "5000.00"), _link(m24, "OID-7852-316766", "10000.00")
    assert (a.net_profit_received, b.net_profit_received) == (D("127.34"), D("254.68"))
    assert a.net_profit_received + b.net_profit_received == a.position.net_profit == D("382.02")


def test_ambiguity_is_reported_not_hidden(m24):
    assert sorted(m24.interchangeable) == [("OID-1359-369928", "OID-1359-980923"), ("OID-2782-346299",)]


def test_mandate_rule(export_0924, rule):
    mand = [p for p in export_0924.positions if in_mandate(p, rule)]
    assert len(mand) == 23 and len({p.oid for p in mand}) == 22


def test_reconciliation_24_sep(m24, stmt_0924, rule, ledger):
    r = reconcile(stmt_0924, m24, rule, ledger)
    assert r.account_balance == D("113839.88")
    assert r.mandate_cash == D("111727.14")
    assert r.difference == D("2112.74")
    assert r.missing_contributions == []


def test_reconciliation_6_sep_nets_to_zero(export_0906, stmt_0906, rule, ledger):
    r = reconcile(stmt_0906, match(export_0906, stmt_0906), rule, ledger)
    assert r.mandate_cash == D("74725.37") and r.difference == D("0.00")


def test_mandate_realised_profit(m24, rule):
    # 6,202.83 (Excel) - 105.46 (entry errors) + 477.40 (delayed note) + 152.37 (early repayment)
    realised = sum((l.net_profit_received for l in m24.links if in_mandate(l.position, rule)), D("0"))
    assert realised == D("6727.14")


def test_mismatched_pair_rejected(export_0924, stmt_0906):
    with pytest.raises(Rejected, match="after the statement ends"):
        match(export_0924, stmt_0906)
