from datetime import date
from decimal import Decimal

import pytest

from conftest import AWAED, MANAFA
from rikz.batch import BatchRejected, parse_batch
from rikz.cli import main


def files(*paths):
    return [(p.name, p.read_bytes()) for p in paths]


def test_full_batch(rule, ledger):
    b = parse_batch(
        files(MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx", *sorted(AWAED.glob("*.pdf"))),
        rule=rule, ledger=ledger)
    assert b.as_of == date(2026, 9, 24)
    assert b.reconciliation.difference == Decimal("2112.74")
    assert len(b.awaed) == 20 and b.chain is not None


def test_export_alone_rejected(rule, ledger):
    with pytest.raises(BatchRejected, match="no date of its own"):
        parse_batch(files(MANAFA / "2026-09-24_portfolio.xlsx"), rule=rule, ledger=ledger)


def test_one_bad_file_rejects_the_whole_batch(rule, ledger):
    good = files(AWAED / "01-murabaha_confirmation.pdf")
    with pytest.raises(BatchRejected) as exc:
        parse_batch(good + [("x.txt", b"nope")], rule=rule, ledger=ledger)
    assert len(exc.value.reasons) == 1


def test_duplicate_upload_counted_once(rule, ledger):
    p = AWAED / "01-murabaha_confirmation.pdf"
    b = parse_batch(files(p, p), rule=rule, ledger=ledger, as_of=date(2026, 1, 8))
    assert len(b.awaed) == 1 and "counted once" in b.notes[0]


def test_cli(capsys):
    assert main(["parse", str(MANAFA / "2026-09-24_portfolio.xlsx"), str(MANAFA / "2026-09-24_account_statement.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "mandate cash 111,727.14" in out
    assert main(["parse", str(MANAFA / "2026-09-24_portfolio.xlsx")]) == 2
    assert "Nothing from this batch was imported" in capsys.readouterr().err
