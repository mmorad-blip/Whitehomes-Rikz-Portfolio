"""Link Manafa account-statement rows to portfolio-export positions.

The statement never names the OID, so links are inferred and every inference
is reported:

* Investment rows (MIWD): one per position, same date, amount = -principal.
* Repayment groups: consecutive MIP* rows on one date starting with MIPA
  (principal), followed by MIPP (gross profit), MIPF (fee, or fee rebate) and
  MIPV (VAT). A group with no MIPP row is a partial principal payment.
* A closed position is settled by one group with profit plus any partial
  payments dated between its entry and that settlement, whose principals add up
  to the position's principal. For each OID the settlements' net profit must
  equal the export's net profit.
* Among all assignments that satisfy those rules, the one with the smallest
  total gap between maturity and payment date is used. When several
  assignments tie, the positions involved are reported as interchangeable:
  they have identical principal, dates and profit, so every metric is the same
  whichever way round they are taken.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..errors import Rejected
from ..model import AccountStatement, ManafaPosition, PortfolioExport, StatementRow
from ..vocab import Txn

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class RepaymentGroup:
    rows: tuple[StatementRow, ...]

    @property
    def date(self) -> date:
        return self.rows[0].date

    def _sum(self, *txns: Txn) -> Decimal:
        return sum((r.amount for r in self.rows if r.txn in txns), ZERO)

    @property
    def principal(self) -> Decimal:
        return self._sum(Txn.PRINCIPAL)

    @property
    def gross_profit(self) -> Decimal:
        return self._sum(Txn.PROFIT)

    @property
    def fee(self) -> Decimal:
        """Agency fee net of any rebate (negative)."""
        return self._sum(Txn.FEE, Txn.FEE_REBATE)

    @property
    def vat(self) -> Decimal:
        return self._sum(Txn.VAT)

    @property
    def net_profit(self) -> Decimal:
        return self.gross_profit + self.fee + self.vat

    @property
    def cash(self) -> Decimal:
        return self._sum(Txn.PRINCIPAL, Txn.PROFIT, Txn.FEE, Txn.FEE_REBATE, Txn.VAT)

    @property
    def is_settlement(self) -> bool:
        return any(r.txn is Txn.PROFIT for r in self.rows)

    @property
    def refs(self) -> str:
        return f"{self.rows[0].ref}..{self.rows[-1].ref}" if len(self.rows) > 1 else self.rows[0].ref


@dataclass(frozen=True)
class PositionLink:
    position: ManafaPosition
    investment: StatementRow
    settlement: RepaymentGroup | None = None
    partials: tuple[RepaymentGroup, ...] = ()

    @property
    def principal_received(self) -> Decimal:
        s = self.settlement.principal if self.settlement else ZERO
        return s + sum((g.principal for g in self.partials), ZERO)

    @property
    def net_profit_received(self) -> Decimal:
        return self.settlement.net_profit if self.settlement else ZERO

    @property
    def paid_on(self) -> date | None:
        return self.settlement.date if self.settlement else None


@dataclass
class Matching:
    links: list[PositionLink]
    interchangeable: list[tuple[str, ...]] = field(default_factory=list)
    unmatched_groups: list[RepaymentGroup] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def link(self, position: ManafaPosition) -> PositionLink:
        return next(l for l in self.links if l.position is position)


def repayment_groups(statement: AccountStatement) -> list[RepaymentGroup]:
    groups: list[list[StatementRow]] = []
    for row in statement.rows:
        if row.txn not in (Txn.PRINCIPAL, Txn.PROFIT, Txn.FEE, Txn.FEE_REBATE, Txn.VAT):
            continue
        prev = groups[-1][-1] if groups else None
        starts_new = row.txn is Txn.PRINCIPAL or prev is None or prev.date != row.date
        if starts_new:
            if row.txn is not Txn.PRINCIPAL:
                raise Rejected(f"statement row {row.row}: a repayment part appears without its principal row")
            groups.append([row])
        else:
            groups[-1].append(row)
    return [RepaymentGroup(tuple(g)) for g in groups]


def _link_investments(statement: AccountStatement, positions) -> dict[int, StatementRow]:
    rows = [r for r in statement.rows if r.txn is Txn.INVESTMENT]
    used: set[int] = set()
    out = {}
    for p in sorted(positions, key=lambda p: (p.entry, p.sheet, p.row)):
        cand = [i for i, r in enumerate(rows) if i not in used and r.date == p.entry and r.amount == -p.principal]
        if not cand:
            raise Rejected(
                f"position {p.oid} ({p.principal} on {p.entry}) has no investment row on the account "
                "statement; the export and statement do not belong together"
            )
        used.add(cand[0])
        out[id(p)] = rows[cand[0]]
    extra = [rows[i] for i in range(len(rows)) if i not in used]
    if extra:
        r = extra[0]
        raise Rejected(
            f"statement row {r.row} invests {-r.amount} on {r.date} but no position in the export matches it"
        )
    return out


def match(export: PortfolioExport, statement: AccountStatement) -> Matching:
    positions = export.positions
    late = [p for p in positions if p.entry > statement.period_end]
    if late:
        raise Rejected(
            f"the export has a position entered on {late[0].entry}, after the statement ends "
            f"({statement.period_end}); upload the export with the statement from the same day"
        )
    investments = _link_investments(statement, positions)

    groups = repayment_groups(statement)
    settles = [g for g in groups if g.is_settlement]
    partials = [g for g in groups if not g.is_settlement]

    # Candidate (settlement index, partial indices) per closed position.
    def candidates(p: ManafaPosition):
        out = []
        for i, s in enumerate(settles):
            if s.date < p.entry:
                continue
            need = p.principal - s.principal
            if need < 0:
                continue
            pool = [j for j, q in enumerate(partials) if p.entry <= q.date <= s.date]
            for k in range(len(pool) + 1):
                for sub in itertools.combinations(pool, k):
                    if sum((partials[j].principal for j in sub), ZERO) == need:
                        out.append((i, sub))
        return out

    closed_by_oid: dict[str, list[ManafaPosition]] = defaultdict(list)
    for p in export.closed:
        closed_by_oid[p.oid].append(p)

    options: dict[str, list] = {}
    for oid, ps in closed_by_oid.items():
        cands = [candidates(p) for p in ps]
        opts = []
        for combo in itertools.product(*cands):
            ss = [c[0] for c in combo]
            pp = [j for c in combo for j in c[1]]
            if len(set(ss)) < len(ss) or len(set(pp)) < len(pp):
                continue
            # Split OIDs repeat the OID's total net profit on each closed row.
            if sum((settles[i].net_profit for i in ss), ZERO) != ps[0].net_profit:
                continue
            cost = sum(abs((settles[c[0]].date - p.maturity).days) for p, c in zip(ps, combo))
            opts.append((cost, tuple(settles[c[0]].date for c in combo), combo))
        if not opts:
            p = ps[0]
            raise Rejected(
                f"closed position {oid} ({p.principal}, net profit {p.net_profit}) has no repayment on the "
                "account statement that matches its principal and net profit"
            )
        options[oid] = [(c, combo) for c, _, combo in sorted(opts, key=lambda o: (o[0], o[1]))]

    order = sorted(options, key=lambda o: (len(options[o]), o))
    best: list = [None]
    solutions: list[dict] = []

    def search(k: int, used_s: frozenset, used_p: frozenset, cost: int, assign: dict):
        if best[0] is not None and cost > best[0]:
            return
        if k == len(order):
            if best[0] is None or cost < best[0]:
                best[0] = cost
                solutions.clear()
            solutions.append(dict(assign))
            return
        oid = order[k]
        for c, combo in options[oid]:
            ss = {x[0] for x in combo}
            pp = {j for x in combo for j in x[1]}
            if ss & used_s or pp & used_p:
                continue
            assign[oid] = combo
            search(k + 1, used_s | ss, used_p | pp, cost + c, assign)
            del assign[oid]

    search(0, frozenset(), frozenset(), 0, {})
    if not solutions:
        raise Rejected("the closed positions cannot all be matched to distinct repayments on the statement")
    chosen = solutions[0]

    varying = sorted(o for o in options if any(s[o] != chosen[o] for s in solutions))
    interchangeable = _group_interchangeable(varying, closed_by_oid, chosen, solutions, settles)

    links: list[PositionLink] = []
    used_s: set[int] = set()
    used_p: set[int] = set()
    for oid, ps in closed_by_oid.items():
        for p, (i, sub) in zip(ps, chosen[oid]):
            used_s.add(i)
            used_p.update(sub)
            links.append(PositionLink(p, investments[id(p)], settles[i], tuple(partials[j] for j in sub)))

    notes: list[str] = []
    # Remaining partial payments belong to open troubled positions (delayed or
    # defaulted notes paying back in instalments). Assign only when exactly one
    # such position could have received it.
    open_partials: dict[int, list[RepaymentGroup]] = defaultdict(list)
    unmatched: list[RepaymentGroup] = [settles[i] for i in range(len(settles)) if i not in used_s]
    for j, q in enumerate(partials):
        if j in used_p:
            continue
        fits = [p for p in export.open if p.status.troubled and p.entry <= q.date and q.principal <= p.principal]
        if len(fits) == 1:
            open_partials[id(fits[0])].append(q)
            notes.append(f"partial principal {q.principal} on {q.date} assigned to {fits[0].oid} ({fits[0].status.value})")
        else:
            unmatched.append(q)
    for p in export.open:
        got = tuple(open_partials.get(id(p), ()))
        if sum((g.principal for g in got), ZERO) >= p.principal:
            raise Rejected(f"open position {p.oid} has received its full principal but is still listed as open")
        links.append(PositionLink(p, investments[id(p)], None, got))

    for g in unmatched:
        notes.append(
            f"repayment {g.refs} on {g.date} (principal {g.principal}, net profit {g.net_profit}) "
            "is not linked to any position"
        )
    order_key = {id(p): n for n, p in enumerate(positions)}
    links.sort(key=lambda l: order_key[id(l.position)])
    return Matching(links, interchangeable, unmatched, notes)


def _group_interchangeable(varying, closed_by_oid, chosen, solutions, settles) -> list[tuple[str, ...]]:
    """Group OIDs whose repayments swap between equally good assignments."""
    parent = {o: o for o in varying}

    def find(o):
        while parent[o] != o:
            o = parent[o]
        return o

    for sol in solutions:
        for a in varying:
            for b in varying:
                if a < b and {x[0] for x in sol[a]} & {x[0] for x in chosen[b]}:
                    parent[find(b)] = find(a)
    groups: dict[str, list[str]] = defaultdict(list)
    for o in varying:
        groups[find(o)].append(o)
    return [tuple(sorted(g)) for g in groups.values()]
