"""Mandate classification and the reconciliation of mandate cash against the
Manafa account balance."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import Contribution, MandateRule
from ..model import AccountStatement, ManafaPosition, StatementRow
from ..vocab import Txn
from .matching import Matching

ZERO = Decimal("0.00")


def in_mandate(p: ManafaPosition, rule: MandateRule) -> bool:
    return p.entry >= rule.funded_from and p.principal >= rule.min_principal


@dataclass
class Reconciliation:
    as_of: object
    account_balance: Decimal
    mandate_cash: Decimal
    contributions: Decimal
    invested: Decimal
    returned: Decimal
    outside: dict[str, Decimal] = field(default_factory=dict)
    missing_contributions: list[Contribution] = field(default_factory=list)

    @property
    def difference(self) -> Decimal:
        return self.account_balance - self.mandate_cash

    def lines(self) -> list[str]:
        out = [
            f"Account balance {self.account_balance:,.2f} = mandate cash {self.mandate_cash:,.2f} "
            f"+ outside the mandate {self.difference:,.2f}",
            f"  mandate cash = contributions {self.contributions:,.2f} - invested {-self.invested:,.2f} "
            f"+ returned {self.returned:,.2f}",
        ]
        for k, v in self.outside.items():
            if v:
                out.append(f"  outside the mandate, {k}: {v:,.2f}")
        return out


def reconcile(
    statement: AccountStatement,
    matching: Matching,
    rule: MandateRule,
    ledger: list[Contribution],
) -> Reconciliation:
    """Mandate cash is built only from statement rows: the ledger's Manafa
    contributions (each must appear as a deposit row), investment rows of
    mandate positions, and repayment rows linked to mandate positions.
    Everything else on the statement is outside the mandate."""
    as_of = statement.period_end
    contribs = [c for c in ledger if c.channel == "manafa" and c.date <= as_of]

    deposit_rows = [r for r in statement.rows if r.txn is Txn.DEPOSIT]
    used: set[int] = set()
    mandate_rows: set[int] = set()
    missing = []
    for c in contribs:
        hit = next((r for r in deposit_rows if r.row not in used and r.date == c.date and r.amount == c.amount), None)
        if hit is None:
            missing.append(c)
        else:
            used.add(hit.row)
            mandate_rows.add(hit.row)

    invested = returned = ZERO
    for link in matching.links:
        if not in_mandate(link.position, rule):
            continue
        mandate_rows.add(link.investment.row)
        invested += link.investment.amount
        for g in ((link.settlement,) if link.settlement else ()) + link.partials:
            mandate_rows.update(r.row for r in g.rows)
            returned += g.cash

    contributed = sum((c.amount for c in contribs if c not in missing), ZERO)
    outside: dict[str, Decimal] = {}
    labels = {
        Txn.DEPOSIT: "deposits",
        Txn.REWARD: "rewards",
        Txn.REDEPOSIT: "re-deposits",
        Txn.WITHDRAWAL: "withdrawals",
        Txn.INVESTMENT: "investments",
    }
    for r in statement.rows:
        if r.row in mandate_rows:
            continue
        key = labels.get(r.txn, "repayments")
        outside[key] = outside.get(key, ZERO) + r.amount

    rec = Reconciliation(
        as_of=as_of,
        account_balance=statement.closing,
        mandate_cash=contributed + invested + returned,
        contributions=contributed,
        invested=invested,
        returned=returned,
        outside=outside,
        missing_contributions=missing,
    )
    assert rec.mandate_cash + sum(outside.values(), ZERO) == statement.closing - statement.opening
    return rec
