"""Awaed Alosool Capital murabaha confirmation (one PDF per deposit)."""

from __future__ import annotations

import io
import re

from .. import dates
from ..errors import Rejected
from ..model import AwaedDeposit, Source
from ..money import parse_pdf_sar
from ..vocab import AWAED_TENOR_DAYS

_AMOUNT = r"-?[\d,]+(?:\.\d+)? SAR"
# label -> shape of the value that follows it on the same line. The footer also
# contains "VAT No. ..." which must not be mistaken for the VAT line.
REQUIRED = {
    "Order ID": r"\S+",
    "Order Date": r"(?:AM|PM) .+",
    "Investment amount": _AMOUNT,
    "Total Return": _AMOUNT,
    "Fees": _AMOUNT,
    "VAT": _AMOUNT,
    "Total Amount": _AMOUNT,
}


def pdf_text(data: bytes) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:  # pdfminer raises many types on damaged files
        raise Rejected(f"the PDF could not be read ({type(exc).__name__})") from None


def looks_like(text: str) -> bool:
    return "Awaed Alosool Capital" in text and "Murabaha Confirmation" in text


def parse(source: Source, text: str) -> AwaedDeposit:
    if not looks_like(text):
        raise Rejected("not an Awaed murabaha confirmation")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    fields: dict[str, str] = {}
    for label, shape in REQUIRED.items():
        pattern = re.compile(rf"^{re.escape(label)} ({shape})$")
        hits = [m.group(1) for ln in lines if (m := pattern.match(ln))]
        if len(hits) != 1:
            what = "missing" if not hits else "printed more than once"
            raise Rejected(f"the confirmation's {label!r} is {what}")
        fields[label] = hits[0]

    products = [ln for ln in lines if ln.endswith("Murabaha") and ln != "Murabaha Confirmation"]
    if len(products) != 1:
        raise Rejected("could not find the product line (e.g. '1 Week Murabaha')")
    product = products[0]
    if product not in AWAED_TENOR_DAYS:
        raise Rejected(f"unknown Awaed product {product!r}; its tenor must be confirmed before import")

    if not re.fullmatch(r"\d+", fields["Order ID"]):
        raise Rejected(f"order ID {fields['Order ID']!r} is not a number")

    principal = parse_pdf_sar(fields["Investment amount"])
    fees = parse_pdf_sar(fields["Fees"])
    vat = parse_pdf_sar(fields["VAT"])
    total_amount = parse_pdf_sar(fields["Total Amount"])
    if total_amount != principal + fees + vat:
        raise Rejected(
            f"Total Amount {total_amount} does not equal investment {principal} + fees {fees} + VAT {vat}"
        )
    total_return = parse_pdf_sar(fields["Total Return"])
    if principal <= 0 or total_return < 0:
        raise Rejected("investment amount must be positive and total return not negative")

    return AwaedDeposit(
        source=source,
        order_id=fields["Order ID"],
        ordered_at=dates.parse_awaed_order(fields["Order Date"]),
        product=product,
        tenor_days=AWAED_TENOR_DAYS[product],
        principal=principal,
        total_return=total_return,
        fees=fees,
        vat=vat,
        total_amount=total_amount,
    )
