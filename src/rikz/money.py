"""Money is always Decimal. Floats from spreadsheets are converted via repr so
that 74725.37 stays 74725.37 and never becomes 74725.369999..."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .errors import Rejected

CENT = Decimal("0.01")
_SAR = re.compile(r"^\s*SAR\s+(-?[\d,]+(?:\.\d+)?)\s*$")
_PDF_SAR = re.compile(r"^\s*(-?[\d,]+(?:\.\d+)?)\s+SAR\s*$")
_GROUPED = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


def _grouped(num: str, raw: str) -> Decimal:
    if not _GROUPED.match(num):
        raise Rejected(f"amount {raw!r} has misplaced thousands separators")
    try:
        return Decimal(num.replace(",", ""))
    except InvalidOperation:
        raise Rejected(f"amount {raw!r} is not a number") from None


def parse_sar(raw: object) -> Decimal:
    """'SAR 1,000.00' -> Decimal('1000.00') (portfolio export)."""
    if not isinstance(raw, str) or not (m := _SAR.match(raw)):
        raise Rejected(f"expected an amount like 'SAR 1,000.00', got {raw!r}")
    return _grouped(m.group(1), raw)


def parse_pdf_sar(raw: str) -> Decimal:
    """'101,684.00 SAR' -> Decimal('101684.00'); keeps every printed decimal."""
    if not (m := _PDF_SAR.match(raw)):
        raise Rejected(f"expected an amount like '1,000.00 SAR', got {raw!r}")
    return _grouped(m.group(1), raw)


def from_cell(raw: object) -> Decimal:
    """A numeric spreadsheet cell holding a riyal amount (at most 2 decimals)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise Rejected(f"expected a number, got {raw!r}")
    value = Decimal(repr(raw))
    if value != value.quantize(CENT):
        raise Rejected(f"amount {raw!r} has more than 2 decimals")
    return value.quantize(CENT)


def parse_percent(raw: object) -> Decimal:
    """'3.60%' -> Decimal('0.0360')."""
    if not isinstance(raw, str) or not re.fullmatch(r"\s*\d+(?:\.\d+)?%\s*", raw):
        raise Rejected(f"expected a percentage like '3.60%', got {raw!r}")
    return Decimal(raw.strip().rstrip("%")) / 100


def fmt(amount: Decimal) -> str:
    return f"{amount.quantize(CENT):,.2f}"
