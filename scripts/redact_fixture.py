"""Make a redacted copy of a real statement for use as a test fixture.

Usage:
    python scripts/redact_fixture.py SOURCE DEST

Manafa account statements (كشف الحساب): the customer block (name, ID, IBAN,
national address) is replaced with a placeholder. The workbook is rewritten by
openpyxl, so embedded images and document properties are dropped too.

Awaed murabaha confirmations: the client tax number is replaced with a
placeholder of the same length and the PDF is rebuilt from the page's
reachable objects only, so no copy of the original text stream survives.

Portfolio exports hold no personal data; they are rewritten to drop metadata.
Never commit an original statement: only the output of this script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLACEHOLDER = "[REDACTED]"
TAX_PLACEHOLDER = b"300000000000003"
CUSTOMER_LABELS = ("اسم العميل", "رقم الهوية", "رقم الآيبان", "العنوان الوطني")


def _utf16(s: bytes) -> bytes:
    return b"".join(b"\x00" + bytes([c]) for c in s)


def redact_xlsx(src: Path, dest: Path) -> None:
    import openpyxl

    wb = openpyxl.load_workbook(src)
    found = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            label = row[0].value
            if isinstance(label, str) and label.strip() in CUSTOMER_LABELS:
                for cell in row[1:]:
                    if cell.value is not None:
                        cell.value = PLACEHOLDER
                found += 1
        ws._images = []
    wb.properties.creator = None
    wb.properties.lastModifiedBy = None
    wb.properties.title = None
    if found and found != len(CUSTOMER_LABELS):
        raise SystemExit(f"expected {len(CUSTOMER_LABELS)} customer rows, found {found}")
    wb.save(dest)


def redact_pdf(src: Path, dest: Path) -> None:
    import pypdf
    from pypdf.generic import DecodedStreamObject, NameObject

    reader = pypdf.PdfReader(src)
    writer = pypdf.PdfWriter()
    pattern = re.compile(re.escape(_utf16(b"Tax Number : ")) + rb"((?:\x00\d){15})")
    for page in reader.pages:
        data = page.get_contents().get_data()
        data, n = pattern.subn(lambda m: _utf16(b"Tax Number : ") + _utf16(TAX_PLACEHOLDER), data)
        if n != 1:
            raise SystemExit(f"expected one client tax number on the page, found {n}")
        new = writer.add_page(page)
        stream = DecodedStreamObject()
        stream.set_data(data)
        new[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    writer.add_metadata({"/Producer": "redacted fixture"})
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    with open(dest, "wb") as fh:
        writer.write(fh)


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(__doc__)
    src, dest = Path(argv[0]), Path(argv[1])
    if src.suffix.lower() == ".pdf":
        redact_pdf(src, dest)
    elif src.suffix.lower() == ".xlsx":
        redact_xlsx(src, dest)
    else:
        raise SystemExit(f"unsupported file type: {src.suffix}")


if __name__ == "__main__":
    main(sys.argv[1:])
