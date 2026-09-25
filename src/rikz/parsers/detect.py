"""Identify a file's channel and kind from its content, then parse it."""

from __future__ import annotations

import hashlib
import io
import zipfile

from ..errors import Rejected
from ..model import Source
from . import awaed_pdf, manafa_account, manafa_portfolio


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_file(name: str, data: bytes):
    """Return a parsed record (AwaedDeposit, PortfolioExport or AccountStatement)
    or raise Rejected with a plain-English reason."""
    digest = sha256(data)
    try:
        if data.startswith(b"%PDF"):
            text = awaed_pdf.pdf_text(data)
            if awaed_pdf.looks_like(text):
                return awaed_pdf.parse(Source(digest, name, "awaed", "murabaha_confirmation"), text)
            raise Rejected("this PDF is not a recognised statement (expected an Awaed murabaha confirmation)")

        if data.startswith(b"PK"):
            import openpyxl

            try:
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True)
            except (zipfile.BadZipFile, KeyError, ValueError, OSError):
                raise Rejected("this spreadsheet could not be opened") from None
            if manafa_portfolio.looks_like(wb.sheetnames):
                return manafa_portfolio.parse(Source(digest, name, "manafa", "portfolio_export"), wb)
            if len(wb.worksheets) == 1 and manafa_account.looks_like(wb.worksheets[0]):
                return manafa_account.parse(Source(digest, name, "manafa", "account_statement"), wb.worksheets[0])
            raise Rejected(
                "this spreadsheet is neither a Manafa portfolio export nor a Manafa account statement"
            )

        raise Rejected("unsupported file type; expected an Awaed PDF or a Manafa .xlsx")
    except Rejected as exc:
        raise Rejected(exc.reason, file=name) from None
