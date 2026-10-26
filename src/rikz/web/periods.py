"""Calendar-period performance (month, quarter, year to date, 12 months).

Figures come from the dated positions in a report, so they depend on the
calendar, not on when statements were uploaded. Each period is compared with
the same stretch of the period before it (October 1–26 against September 1–26).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal(0)
YEAR_DAYS = Decimal(365)

PERIODS = (("month", "Month"), ("quarter", "Quarter"), ("ytd", "Year to date"), ("12m", "12 months"))
DEFAULT = "month"


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def shift(d: date, months: int) -> date:
    """Move by whole months; a month end stays a month end."""
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, last if d == _month_end(d) else min(d.day, last))


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        s, e = self.start, self.end
        whole_months = s.day == 1 and e == _month_end(e)
        if whole_months and (s.year, s.month) == (e.year, e.month):
            return s.strftime("%b %Y")
        if whole_months and s.year == e.year and s.month == 1 and e.month == 12:
            return str(s.year)
        if whole_months and s.year == e.year and s.month % 3 == 1 and e.month == s.month + 2:
            return f"Q{(s.month - 1) // 3 + 1} {s.year}"
        if whole_months:
            return f"{s:%b %Y} – {e:%b %Y}" if s.year != e.year else f"{s:%b} – {e:%b %Y}"
        if s.year == e.year:
            return f"{s:%d %b} – {e:%d %b %Y}"
        return f"{s:%d %b %Y} – {e:%d %b %Y}"


def windows(kind: str, as_of: date) -> tuple[Window, Window]:
    first = as_of.replace(day=1)
    if kind == "quarter":
        start, step = first.replace(month=(as_of.month - 1) // 3 * 3 + 1), 3
        if start == first and as_of != _month_end(as_of):
            # In a quarter's first month, quarter-to-date is just the month;
            # report the last full quarter against the one before it instead.
            end = start - timedelta(days=1)
            start = shift(start, -3)
            return Window(start, end), Window(shift(start, -3), start - timedelta(days=1))
    elif kind == "ytd":
        start, step = date(as_of.year, 1, 1), 12
    elif kind == "12m":
        start, step = shift(first, -11), 12
    else:
        start, step = first, 1
    prev_start = shift(start, -step)
    prev_end = min(shift(as_of, -step), start - timedelta(days=1))
    return Window(start, as_of), Window(prev_start, prev_end)


def _d(v) -> date | None:
    return date.fromisoformat(v) if isinstance(v, str) else v


def _overlap(a: date, b: date, w: Window) -> int:
    """Days of the half-open span [a, b) inside the window."""
    lo, hi = max(a, w.start), min(b, w.end + timedelta(days=1))
    return max(0, (hi - lo).days)


def inception(holdings: list[dict]) -> date | None:
    starts = [_d(h["start"]) for h in holdings if h.get("start")]
    return min(starts) if starts else None


def covered(holdings: list[dict], w: Window) -> bool:
    """Whether the portfolio existed during the window."""
    first = inception(holdings)
    return first is not None and first <= w.end


def measure(holdings: list[dict], as_of: date, w: Window) -> dict:
    """Flows and returns of the positions inside one window."""
    first = inception(holdings)
    if first and w.start < first <= w.end:
        # Average capital over the days the portfolio existed, not the empty run-up.
        w = Window(first, w.end)
    realised = earned = capital_days = deployed = closed_principal = ZERO
    opened = closed = 0
    for h in holdings:
        start, maturity = _d(h["start"]), _d(h["maturity"])
        paid_on = _d(h.get("paid_on"))
        principal = Decimal(h["principal"])
        state = h["state"]
        if state == "closed":
            ended = paid_on or maturity
            profit = Decimal(h["realised_net"])
            if w.start <= ended <= w.end:
                realised += profit
                closed += 1
                closed_principal += principal
        else:
            ended = as_of + timedelta(days=1)
            # Delayed and defaulted positions tie up capital but earn nothing.
            profit = Decimal(h["expected_net"]) if state == "active" else ZERO
        contract = max(1, (maturity - start).days)
        earned += profit * _overlap(start, maturity, w) / contract if profit else ZERO
        capital_days += principal * _overlap(start, ended, w)
        if w.start <= start <= w.end:
            deployed += principal
            opened += 1
    avg_capital = capital_days / w.days if w.days else ZERO
    return {
        "realised": realised,
        "earned": earned,
        "yield": earned / capital_days * YEAR_DAYS if capital_days else None,
        "avg_capital": avg_capital,
        "deployed": deployed,
        "opened": opened,
        "closed": closed,
        "closed_principal": closed_principal,
    }


def contributed(monthly: list[dict], w: Window) -> Decimal:
    total = ZERO
    for r in monthly:
        m = _d(r["month"])
        if w.start <= m <= w.end or (m <= w.start <= _month_end(m)):
            total += Decimal(r["contributed"])
    return total


def monthly_series(holdings: list[dict], as_of: date, months: int = 12) -> list[dict]:
    """One measurement per calendar month, oldest first, for sparklines."""
    out = []
    for i in range(months - 1, -1, -1):
        s = shift(as_of.replace(day=1), -i)
        out.append(measure(holdings, as_of, Window(s, min(_month_end(s), as_of))))
    return out


def point_at(versions: list, current: int, before: date) -> tuple[int, date, dict] | None:
    """The newest report dated before a day, for point-in-time comparisons."""
    best = None
    for ver, as_of, _, head in versions:
        if ver > current or as_of is None or _d(as_of) >= before:
            continue
        key = (_d(as_of), ver)
        if best is None or key > best[0]:
            best = (key, (ver, _d(as_of), head or {}))
    return best[1] if best else None
