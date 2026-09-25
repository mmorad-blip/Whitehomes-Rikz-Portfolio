"""Chain Awaed deposits into a wallet timeline and flag gaps.

Awaed sends a confirmation per deposit but no account statement, so the
wallet is derived: ledger contributions in, principal out on the order date,
principal + total return back on maturity (order date + contract days).
Nothing is plugged. Where the confirmations do not chain, the gap is flagged:

* rounding: a deposit needs less than 1 SAR more than the wallet holds.
  Awaed rounds rolled-over principal up to the whole riyal, so the derived
  wallet dips below zero by a few halalas; shown, not corrected;
* shortfall: a deposit needs 1 SAR or more than the wallet holds on its
  order date (a missing confirmation, a missing contribution, or a platform
  credit the confirmations do not show);
* idle: money back from a maturity waits more than `idle_days` before the
  next deposit;
* uninvested at as-of: a deposit matured, no later confirmation exists and
  the money is still in the wallet.

A deposit is taken to have matured on its maturity date: its principal and
return count as realised and sit in the wallet as cash until a later
confirmation shows them rolled into a new deposit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..config import Contribution
from ..errors import Rejected
from ..model import AwaedDeposit

ZERO = Decimal("0")
ROUNDING_LIMIT = Decimal("1.00")


@dataclass(frozen=True)
class WalletEvent:
    date: date
    kind: str  # contribution | deposit | maturity
    amount: Decimal
    balance: Decimal
    ref: str


@dataclass(frozen=True)
class Gap:
    kind: str  # rounding | shortfall | idle | uninvested
    date: date
    amount: Decimal
    detail: str


@dataclass
class Chain:
    as_of: date
    deposits: list[AwaedDeposit]
    events: list[WalletEvent]
    gaps: list[Gap] = field(default_factory=list)

    @property
    def wallet(self) -> Decimal:
        return self.events[-1].balance if self.events else ZERO

    @property
    def outstanding(self) -> list[AwaedDeposit]:
        return [d for d in self.deposits if d.order_date <= self.as_of < d.maturity]

    @property
    def matured(self) -> list[AwaedDeposit]:
        return [d for d in self.deposits if d.maturity <= self.as_of]


def build(
    deposits: list[AwaedDeposit],
    ledger: list[Contribution],
    as_of: date,
    idle_days: int = 3,
) -> Chain:
    seen: dict[str, AwaedDeposit] = {}
    for d in deposits:
        if d.order_id in seen and seen[d.order_id].source.sha256 != d.source.sha256:
            raise Rejected(f"two different confirmations carry Awaed order ID {d.order_id}")
        seen[d.order_id] = d
    deps = sorted(seen.values(), key=lambda d: (d.ordered_at, d.order_id))
    deps = [d for d in deps if d.order_date <= as_of]

    # Same-day order: contributions and maturities land before new deposits.
    day: dict[date, list] = defaultdict(list)
    for c in ledger:
        if c.channel == "awaed" and c.date <= as_of:
            day[c.date].append((0, "contribution", c.amount, f"contribution {c.date}"))
    for d in deps:
        if d.maturity <= as_of:
            day[d.maturity].append((1, "maturity", d.principal + d.total_return, f"order {d.order_id}"))
        day[d.order_date].append((2, "deposit", -d.principal, f"order {d.order_id}"))

    events: list[WalletEvent] = []
    gaps: list[Gap] = []
    balance = ZERO
    for when in sorted(day):
        for _, kind, amount, ref in sorted(day[when], key=lambda e: e[0]):
            balance += amount
            events.append(WalletEvent(when, kind, amount, balance, ref))
            if kind == "deposit" and balance < 0:
                gaps.append(
                    Gap("rounding" if -balance < ROUNDING_LIMIT else "shortfall", when, -balance,
                        f"{ref} needs {-balance:,.2f} SAR more than the wallet holds on {when:%d %b %Y}")
                )

    # Idle cash: from each maturity to the next deposit (or as-of).
    starts = sorted(d.order_date for d in deps)
    for d in deps:
        if d.maturity > as_of:
            continue
        nxt = next((s for s in starts if s >= d.maturity), None)
        if nxt is None:
            gaps.append(
                Gap("uninvested", d.maturity, d.principal + d.total_return,
                    f"order {d.order_id} matured on {d.maturity:%d %b %Y} and is not yet rolled over: "
                    f"{d.principal + d.total_return:,.2f} SAR held as wallet cash on {as_of:%d %b %Y} "
                    "until a new confirmation is uploaded")
            )
        elif (nxt - d.maturity).days > idle_days:
            gaps.append(
                Gap("idle", d.maturity, d.principal + d.total_return,
                    f"order {d.order_id} matured on {d.maturity:%d %b %Y}; the next deposit was on "
                    f"{nxt:%d %b %Y} ({(nxt - d.maturity).days} days later)")
            )
    gaps.sort(key=lambda g: g.date)
    return Chain(as_of, deps, events, gaps)
