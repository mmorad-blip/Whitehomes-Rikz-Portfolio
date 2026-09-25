"""Nothing about a real person or account may enter the repo. These checks run
over every committed fixture and every text file in the repository."""

import re
import subprocess
import zipfile
import zlib
from pathlib import Path

import openpyxl
import pytest

from conftest import AWAED, FIXTURES, MANAFA

ROOT = Path(__file__).resolve().parents[1]
IBAN = re.compile(r"SA\d{2}\s?(?:\d{4}\s?){4}\d{4}|SA\d{22}")
SAUDI_ID = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")
# 15-digit VAT numbers allowed in fixtures: the redaction placeholder and
# Awaed's own published VAT number from its footer.
ALLOWED_VAT = {"300000000000003", "311514457500003"}


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(z.read(n).decode("utf8", "ignore") for n in z.namelist())


def _pdf_streams(path: Path) -> list[bytes]:
    raw = path.read_bytes()
    out = [raw]
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            pass
    return out


def _utf16_to_ascii(b: bytes) -> str:
    return b.replace(b"\x00", b"").decode("latin1")


@pytest.mark.parametrize("path", sorted(MANAFA.glob("*_account_statement.xlsx")), ids=lambda p: p.name)
def test_customer_block_redacted(path):
    ws = openpyxl.load_workbook(path).worksheets[0]
    labels = ("اسم العميل", "رقم الهوية", "رقم الآيبان", "العنوان الوطني")
    rows = [r for r in ws.iter_rows(values_only=True) if isinstance(r[0], str) and r[0].strip() in labels]
    assert len(rows) == 4
    for r in rows:
        assert all(v in (None, "[REDACTED]") for v in r[1:])


@pytest.mark.parametrize("path", sorted(FIXTURES.rglob("*.xlsx")), ids=lambda p: p.name)
def test_no_iban_or_id_in_workbooks(path):
    text = _xlsx_text(path)
    assert not IBAN.search(text)
    assert not SAUDI_ID.search(text)
    assert "550387" not in text and "550387" not in path.name  # platform customer number


@pytest.mark.parametrize("path", sorted(AWAED.glob("*.pdf")), ids=lambda p: p.name)
def test_only_placeholder_tax_number_in_pdfs(path):
    for blob in _pdf_streams(path):
        text = _utf16_to_ascii(blob)
        for vat in re.findall(r"(?<!\d)3\d{14}(?!\d)", text):
            assert vat in ALLOWED_VAT
        assert not IBAN.search(text)


def test_no_personal_data_in_tracked_text_files():
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    for name in tracked:
        p = ROOT / name
        if p.suffix in (".xlsx", ".pdf") or not p.is_file():
            continue
        text = p.read_text("utf8", errors="ignore")
        assert not IBAN.search(text), name
        assert not SAUDI_ID.search(text), name


def test_no_original_statements_tracked():
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    binaries = [n for n in tracked if n.endswith((".xlsx", ".pdf"))]
    assert all(n.startswith("tests/fixtures/") for n in binaries), binaries
    assert not any("Rikz_Portfolio" in n for n in tracked)
