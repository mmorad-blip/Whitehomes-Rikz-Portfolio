"""XIRR on the same convention as Excel: each flow is discounted by
(1 + r) ** (days since the first flow / 365)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal


class NoSolution(ValueError):
    pass


def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    return sum(a / (1.0 + rate) ** t for t, a in flows)


def xirr(flows: list[tuple[date, Decimal]], days_per_year: int = 365) -> Decimal:
    """Annual rate r such that the flows' present value is zero.

    Bisection on a wide bracket, so the solver can never jump to a spurious root
    the way Newton's method can; then polished to 1e-12."""
    if not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        raise NoSolution("XIRR needs at least one inflow and one outflow")
    d0 = min(d for d, _ in flows)
    fl = [((d - d0).days / days_per_year, float(a)) for d, a in flows]
    lo, hi = -0.9999, 1.0
    while _npv(hi, fl) > 0:
        hi *= 2
        if hi > 1e6:
            raise NoSolution("XIRR has no solution below 1,000,000%")
    if _npv(lo, fl) * _npv(hi, fl) > 0:
        raise NoSolution("XIRR has no sign change in the search range")
    for _ in range(200):
        mid = (lo + hi) / 2
        if _npv(lo, fl) * _npv(mid, fl) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-13:
            break
    r = (lo + hi) / 2
    if not math.isfinite(r):
        raise NoSolution("XIRR did not converge")
    return Decimal(repr(r)).quantize(Decimal("1e-12"))
