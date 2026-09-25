from __future__ import annotations

import io
from pathlib import Path

import openpyxl
import pytest

from rikz.config import load_ledger, load_mandate
from rikz.parsers import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
MANAFA = FIXTURES / "manafa"
AWAED = FIXTURES / "awaed"


def load(path: Path):
    return parse_file(path.name, path.read_bytes())


def xlsx_bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def edited(path: Path, edit) -> bytes:
    """A copy of a fixture workbook with `edit(wb)` applied."""
    wb = openpyxl.load_workbook(path)
    edit(wb)
    return xlsx_bytes(wb)


@pytest.fixture(scope="session")
def rule():
    return load_mandate()


@pytest.fixture(scope="session")
def ledger():
    return load_ledger()


@pytest.fixture(scope="session")
def stmt_0924():
    return load(MANAFA / "2026-09-24_account_statement.xlsx")


@pytest.fixture(scope="session")
def export_0924():
    return load(MANAFA / "2026-09-24_portfolio.xlsx")


@pytest.fixture(scope="session")
def stmt_0906():
    return load(MANAFA / "2026-09-06_account_statement.xlsx")


@pytest.fixture(scope="session")
def export_0906():
    return load(MANAFA / "2026-09-06_portfolio.xlsx")


@pytest.fixture(scope="session")
def awaed():
    return [load(p) for p in sorted(AWAED.glob("*.pdf"))]
