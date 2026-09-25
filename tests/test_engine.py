import json
from datetime import date
from decimal import Decimal as D

import pytest

from conftest import AWAED, MANAFA
from rikz.awaed.chain import build as build_chain
from rikz.batch import parse_batch
from rikz.config import load_settings
from rikz.engine import holdings as H
from rikz.engine.metrics import compute
from rikz.engine.render import load_excel_reference, to_json, to_text
from rikz.engine.run import report_from_batch
from rikz.engine.xirr import NoSolution, xirr
from rikz.errors import Rejected

CENT = D("0.01")
BP = D("0.0001")  # one hundredth of a percentage point


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def batch(rule, ledger):
    files = [MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx", *sorted(AWAED.glob("*.pdf"))]
    return parse_batch([(p.name, p.read_bytes()) for p in files], rule=rule, ledger=ledger)


@pytest.fixture(scope="module")
def report(batch, rule, ledger, settings):
    return report_from_batch(batch, rule=rule, ledger=ledger, settings=settings)


def money(r, key):
    return r.value(key).quantize(CENT)


def pct(r, key):
    return r.value(key).quantize(BP)


def test_xirr_matches_excel():
    # The Excel's own cash flows and NAV on 24 Sep give its XIRR of -3.003762629%.
    flows = [(date(2026, 1, 6), D("-75008.05")), (date(2026, 1, 7), D("-75008.05")), (date(2026, 1, 11), D("-25008.05")),
             (date(2026, 1, 22), D("-40008.05")), (date(2026, 2, 23), D("-35005.25")), (date(2026, 9, 24), D("244849.9899"))]
    assert abs(xirr(flows) - D("-0.03003762629")) < D("1e-9")
    with pytest.raises(NoSolution):
        xirr([(date(2026, 1, 1), D("100"))])


def test_headline_24_sep(report):
    assert money(report, "total.nav") == D("244711.25")
    assert money(report, "awaed.nav") == D("102406.85")
    assert money(report, "manafa.nav") == D("142304.41")
    assert money(report, "total.realised") == D("9133.99")
    assert money(report, "manafa.realised") == D("6727.14")
    assert money(report, "awaed.realised") == D("2406.85")
    assert money(report, "manafa.accrued") == D("577.27")  # 372.49 x 83/96 + 395.59 x 60/93
    assert money(report, "total.wallet") == D("214133.99")
    assert money(report, "total.provision") == D("-15000.00")
    assert money(report, "total.net_gain") == D("-5326.20")


def test_returns_24_sep(report):
    assert pct(report, "total.xirr") == D("-0.0308")
    assert pct(report, "total.xirr_recovery") == D("0.0568")
    assert pct(report, "awaed.xirr") == D("0.0339")
    assert pct(report, "manafa.xirr") == D("-0.0755")
    assert pct(report, "manafa.xirr_recovery") == D("0.0730")


def test_risk_24_sep(report):
    assert pct(report, "credit.troubled_total_nav") == D("0.0613")
    assert pct(report, "total.idle") == D("0.8750")
    assert pct(report, "credit.default_rate_count") == D("0.0455")  # 1 of 22 opportunity IDs
    assert report.value("credit.positions") == 23
    assert report.value("credit.opportunities") == 22
    policy = {row["check"]: row["status"] for row in report.tables["policy"]}
    assert policy["Max troubled exposure (% of total NAV, before provision)"] == "BREACH"
    assert policy["Max idle cash (% of total NAV)"] == "BREACH"


def test_wallets_agree_with_estimates(report):
    assert all(c.endswith("agree") for c in report.checks)


def test_capital_by_owner(report):
    owners = {row["owner"]: row["contributed"] for row in report.tables["capital_by_owner"]}
    assert owners == {"Whitehomes": D("215000.00"), "Rikz": D("35000.00")}


def test_liquidity_ladder_adds_up_to_nav_plus_unaccrued(report):
    ladder = report.tables["liquidity"]
    assert ladder[0]["total"].quantize(CENT) == D("214133.99")
    # Everything due: cash + open principal + full expected profit on open notes.
    assert ladder[-1]["cumulative"].quantize(CENT) == D("244902.07")


def test_open_awaed_deposit_accrues(batch, rule, ledger, settings):
    as_of = date(2026, 9, 20)
    chain = build_chain(batch.awaed, ledger, as_of)
    hs = H.build(None, chain, rule, as_of)
    r = compute(as_of=as_of, holdings=hs, ledger=ledger, wallets={"awaed": chain.wallet}, wallet_refs={}, settings=settings)
    assert r.value("awaed.active") == D("102324.00")
    assert r.value("awaed.accrued") == D("82.56932856") * 5 / 7
    assert money(r, "awaed.wallet") == D("0.28")


def test_every_figure_has_a_basis(report):
    # A zero summed over nothing (e.g. no delayed notes) has nothing to point to.
    missing = [k for k, f in report.figures.items() if not f.basis and f.value]
    assert missing == []
    manafa = [h for h in report.holdings if h.channel == "manafa"]
    assert all(any("statement" in ref for ref in h.refs) for h in manafa)


def test_renderings(report, settings):
    text = to_text(report, settings, load_excel_reference())
    assert "244,711.25" in text and "[Excel: Awaed 102,406.76; Manafa 142,443.23; Total 244,849.99]" in text
    assert "placeholder" in text
    doc = json.loads(to_json(report, settings, load_excel_reference()))
    assert doc["figures"]["total.nav"]["value"].startswith("244711.25")
    assert doc["figures"]["total.nav"]["excel"] == "244849.99"


def test_report_needs_matching_dates(batch, rule, ledger, settings):
    import copy
    stale = copy.copy(batch)
    stale.as_of = date(2026, 9, 30)
    with pytest.raises(Rejected, match="stale"):
        report_from_batch(stale, rule=rule, ledger=ledger, settings=settings)


def test_cli_report(capsys):
    from rikz.cli import main
    files = [str(MANAFA / "2026-09-24_portfolio.xlsx"), str(MANAFA / "2026-09-24_account_statement.xlsx"),
             *map(str, sorted(AWAED.glob("*.pdf")))]
    assert main(["report", *files]) == 0
    assert "Current NAV" in capsys.readouterr().out
