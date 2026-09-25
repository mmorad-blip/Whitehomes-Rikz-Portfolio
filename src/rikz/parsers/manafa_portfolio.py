"""Manafa portfolio export: sheets الاستثمارات القائمة (open) and
الاستثمارات السابقة (closed). Dates are MM.DD.YYYY, amounts 'SAR x,xxx.xx'.
The export carries no date of its own; its as-of date comes from the account
statement it is uploaded with."""

from __future__ import annotations

from dataclasses import replace

from .. import dates
from ..errors import Rejected
from ..model import ManafaPosition, PortfolioExport, Source
from ..money import parse_percent, parse_sar
from ..vocab import manafa_rating, manafa_status, manafa_tenor_months

OPEN_SHEET = "الاستثمارات القائمة"
CLOSED_SHEET = "الاستثمارات السابقة"
COMMON = ("الرقم", "الرقم المرجعي", "المدة", "العائد على المشاركة", "تصنيف الفرصة", "المبلغ", "صافي الربح")
HEADERS = {
    OPEN_SHEET: COMMON + ("اجمالي المبلغ المتوقع استلامه", "تاريخ القيد", "تاريخ الاستحقاق", "حالة المشاركة"),
    CLOSED_SHEET: COMMON + ("اجمالي المبلغ المستلم", "تاريخ القيد", "تاريخ الاستحقاق", "حالة المشاركة"),
}


def looks_like(sheetnames: list[str]) -> bool:
    return OPEN_SHEET in sheetnames or CLOSED_SHEET in sheetnames


def _sheet(source: Source, wb, name: str, want_closed: bool) -> tuple[ManafaPosition, ...]:
    if name not in wb.sheetnames:
        raise Rejected(f"sheet {name!r} is missing")
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise Rejected(f"sheet {name!r} is empty")
    header = tuple((c.strip() if isinstance(c, str) else c) for c in rows[0][: len(HEADERS[name])])
    if header != HEADERS[name]:
        raise Rejected(f"sheet {name!r} does not have the expected column headings")

    out = []
    for i, r in enumerate(rows[1:], start=2):
        if all(v is None for v in r):
            continue
        if any(v is not None for v in r[len(HEADERS[name]):]):
            raise Rejected(f"sheet {name!r} row {i} has values beyond the known columns")
        try:
            number, oid, tenor, rate, rating, amount, net, total, entry, maturity, status = r[:11]
            if not isinstance(number, int):
                raise Rejected(f"row number {number!r} is not a whole number")
            if not isinstance(oid, str) or not oid.startswith("OID-"):
                raise Rejected(f"reference {oid!r} is not an OID")
            pos = ManafaPosition(
                source=source,
                sheet=name,
                row=i,
                number=number,
                oid=oid.strip(),
                tenor_months=manafa_tenor_months(tenor),
                rate=parse_percent(rate),
                rating=manafa_rating(rating),
                principal=parse_sar(amount),
                net_profit=parse_sar(net),
                total=parse_sar(total),
                entry=dates.parse(entry, dates.EXPORT),
                maturity=dates.parse(maturity, dates.EXPORT),
                status=manafa_status(status),
            )
        except Rejected as exc:
            raise Rejected(f"sheet {name!r} row {i}: {exc.reason}") from None
        if pos.status.closed != want_closed:
            raise Rejected(f"sheet {name!r} row {i}: status {status!r} does not belong on this sheet")
        if pos.maturity <= pos.entry:
            raise Rejected(f"sheet {name!r} row {i}: maturity is not after the entry date")
        out.append(pos)
    return tuple(out)


def _number_occurrences(positions: tuple[ManafaPosition, ...], seen: dict) -> tuple[ManafaPosition, ...]:
    out = []
    for p in positions:
        base = (p.oid, p.entry, p.principal)
        seen[base] = seen.get(base, 0) + 1
        out.append(replace(p, occurrence=seen[base]))
    return tuple(out)


def parse(source: Source, wb) -> PortfolioExport:
    seen: dict = {}
    return PortfolioExport(
        source=source,
        open=_number_occurrences(_sheet(source, wb, OPEN_SHEET, want_closed=False), seen),
        closed=_number_occurrences(_sheet(source, wb, CLOSED_SHEET, want_closed=True), seen),
    )
