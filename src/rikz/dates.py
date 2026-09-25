"""Every source has its own date format. Each parser names the format it
expects; a value in any other shape is rejected, never guessed."""

from __future__ import annotations

import re
from datetime import date, datetime

from .errors import Rejected

EXPORT = "MM.DD.YYYY"  # Manafa portfolio export
STATEMENT = "DD/MM/YYYY"  # Manafa account statement rows
HEADER = "YYYY/MM/DD"  # Manafa account statement period line and footer

_FORMATS = {
    EXPORT: (re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$"), ("m", "d", "y")),
    STATEMENT: (re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"), ("d", "m", "y")),
    HEADER: (re.compile(r"^(\d{4})/(\d{2})/(\d{2})$"), ("y", "m", "d")),
}


def parse(raw: object, fmt: str) -> date:
    pattern, order = _FORMATS[fmt]
    if not isinstance(raw, str) or not (m := pattern.match(raw.strip())):
        raise Rejected(f"date {raw!r} is not in the expected {fmt} format")
    parts = dict(zip(order, map(int, m.groups())))
    try:
        return date(parts["y"], parts["m"], parts["d"])
    except ValueError:
        raise Rejected(f"date {raw!r} is not a valid {fmt} date") from None


_AWAED = re.compile(r"^(AM|PM)\s+t(\d{1,2}):(\d{2})\s+(\d{4}-\d{2}-\d{2})$")


def parse_awaed_order(raw: str) -> datetime:
    """Awaed prints order dates as 'PM t01:26 2026-04-14' (12-hour clock)."""
    m = _AWAED.match(raw.strip())
    if not m:
        raise Rejected(f"order date {raw!r} is not in the expected 'PM t01:26 2026-04-14' shape")
    ampm, hh, mm, day = m.groups()
    hour = int(hh)
    if not 1 <= hour <= 12 or int(mm) > 59:
        raise Rejected(f"order date {raw!r} has an invalid time")
    hour = hour % 12 + (12 if ampm == "PM" else 0)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise Rejected(f"order date {raw!r} is not a valid date") from None
    return datetime(d.year, d.month, d.day, hour, int(mm))
