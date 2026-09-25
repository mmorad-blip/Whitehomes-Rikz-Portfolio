"""One list of positions across both channels, valued from statements only.

Each holding keeps references to the statement lines it was built from, so
every figure computed from it can be traced back."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from ..awaed.chain import Chain
from ..config import MandateRule
from ..manafa.mandate import in_mandate
from ..manafa.matching import Matching, PositionLink
from ..model import AwaedDeposit, StatementRow
from ..vocab import Status

ZERO = Decimal("0")


class State(str, Enum):
    ACTIVE = "active"
    DELAYED = "delayed"
    DEFAULTED = "defaulted"
    CLOSED = "closed"


@dataclass(frozen=True)
class Holding:
    channel: str  # awaed | manafa
    ident: str  # Awaed order ID, or Manafa OID (with a split / occurrence suffix)
    oid: str  # opportunity ID (Manafa) or order ID (Awaed); splits share it
    rating: str | None  # Manafa export rating; Awaed confirmations carry none
    state: State
    principal: Decimal
    start: date
    maturity: date
    expected_net: Decimal  # contract net profit
    gross: Decimal | None  # gross profit (paid, or contract for open Manafa)
    fee: Decimal | None  # platform fee incl. rebates (negative) when known
    vat: Decimal | None  # VAT on the fee (negative) when known
    paid_on: date | None  # closed: maturity (Awaed) or payment date (Manafa)
    realised_net: Decimal  # net profit received (closed only)
    principal_recovered: Decimal = ZERO  # partial principal received while open
    last_partial: date | None = None
    refs: tuple[str, ...] = field(default=(), repr=False)

    @property
    def contract_days(self) -> int:
        return (self.maturity - self.start).days

    @property
    def outstanding(self) -> Decimal:
        return ZERO if self.state is State.CLOSED else self.principal - self.principal_recovered

    @property
    def is_split(self) -> bool:
        return "#" in self.ident


def _row_ref(row: StatementRow) -> str:
    return f"Manafa statement {row.source.sha256[:8]} row {row.row} ({row.ref})"


def _manafa(link: PositionLink, split_count: int) -> Holding:
    p = link.position
    ident = p.oid if split_count == 1 else f"{p.oid}#{p.principal:,.0f}" + (f"-{p.occurrence}" if p.occurrence > 1 else "")
    refs = [f"Manafa export {p.source.sha256[:8]} sheet {p.sheet} row {p.row}", _row_ref(link.investment)]
    for g in ((link.settlement,) if link.settlement else ()) + link.partials:
        refs.extend(_row_ref(r) for r in g.rows)
    state = {
        Status.ACTIVE: State.ACTIVE,
        Status.DELAYED: State.DELAYED,
        Status.DEFAULTED: State.DEFAULTED,
        Status.REPAID: State.CLOSED,
        Status.REPAID_EARLY: State.CLOSED,
    }[p.status]
    s = link.settlement
    if state is State.CLOSED:
        expected = s.net_profit
        gross, fee, vat = s.gross_profit, s.fee, s.vat
    else:
        # Open rows carry their own net profit; gross = expected total - principal.
        expected = p.net_profit
        gross, fee, vat = p.total - p.principal, None, None
    partial = sum((g.principal for g in link.partials), ZERO) if state is not State.CLOSED else ZERO
    return Holding(
        channel="manafa",
        ident=ident,
        oid=p.oid,
        rating=p.rating,
        state=state,
        principal=p.principal,
        start=p.entry,
        maturity=p.maturity,
        expected_net=expected,
        gross=gross,
        fee=fee,
        vat=vat,
        paid_on=s.date if s else None,
        realised_net=s.net_profit if s else ZERO,
        principal_recovered=partial,
        last_partial=max((g.date for g in link.partials), default=None) if state is not State.CLOSED else None,
        refs=tuple(refs),
    )


def _awaed(d: AwaedDeposit, as_of: date) -> Holding:
    closed = d.maturity <= as_of
    return Holding(
        channel="awaed",
        ident=d.order_id,
        oid=d.order_id,
        rating=None,
        state=State.CLOSED if closed else State.ACTIVE,
        principal=d.principal,
        start=d.order_date,
        maturity=d.maturity,
        expected_net=d.total_return,
        gross=d.total_return,
        fee=-d.fees,
        vat=-d.vat,
        paid_on=d.maturity if closed else None,
        realised_net=d.total_return if closed else ZERO,
        refs=(f"Awaed confirmation order {d.order_id} ({d.source.sha256[:8]})",),
    )


def build(matching: Matching | None, chain: Chain | None, rule: MandateRule, as_of: date) -> list[Holding]:
    out: list[Holding] = []
    if chain:
        out.extend(_awaed(d, as_of) for d in chain.deposits)
    if matching:
        mandate = [l for l in matching.links if in_mandate(l.position, rule)]
        counts: dict[str, int] = {}
        for l in mandate:
            counts[l.position.oid] = counts.get(l.position.oid, 0) + 1
        out.extend(_manafa(l, counts[l.position.oid]) for l in mandate)
    return out
