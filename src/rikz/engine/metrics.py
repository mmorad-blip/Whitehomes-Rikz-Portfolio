"""Portfolio metrics, following the definitions of the Excel model
(Rikz_Portfolio_V2) but fed only from statements.

Where a definition departs from the Excel, the figure carries a note saying
how and why, and the Excel-method value is kept alongside it."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..config import Contribution, Settings
from .holdings import Holding, State
from .xirr import NoSolution, xirr

ZERO = Decimal("0")
CHANNELS = ("awaed", "manafa")


@dataclass(frozen=True)
class Figure:
    """One reported number: its value, what it is built from and any caveat."""

    key: str
    label: str
    value: Decimal | None
    unit: str  # SAR | ratio | days | count | x
    basis: tuple[str, ...] = ()  # holding idents / ledger lines / other figure keys
    note: str = ""


def _ratio(a: Decimal, b: Decimal) -> Decimal | None:
    return None if b == 0 else a / b


@dataclass
class ChannelNumbers:
    contributed: Decimal = ZERO
    fees: Decimal = ZERO
    realised: Decimal = ZERO
    active: Decimal = ZERO
    delayed: Decimal = ZERO
    defaulted: Decimal = ZERO
    provision: Decimal = ZERO  # negative
    recovered: Decimal = ZERO
    accrued: Decimal = ZERO
    wallet: Decimal = ZERO
    wallet_estimate: Decimal = ZERO

    @property
    def nav(self) -> Decimal:
        return self.active + self.delayed + self.defaulted + self.provision + self.accrued + self.wallet

    @property
    def nav_recovery(self) -> Decimal:
        return self.nav - self.provision

    @property
    def net_gain(self) -> Decimal:
        return self.nav - self.contributed - self.fees

    def __add__(self, o: "ChannelNumbers") -> "ChannelNumbers":
        return ChannelNumbers(*(getattr(self, f) + getattr(o, f) for f in self.__dataclass_fields__))


@dataclass
class Report:
    as_of: date
    holdings: list[Holding]
    channels: dict[str, ChannelNumbers]
    figures: dict[str, Figure] = field(default_factory=dict)
    tables: dict[str, list[dict]] = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, label: str, value, unit: str, basis=(), note: str = "") -> Figure:
        if value is not None and not isinstance(value, Decimal):
            value = Decimal(value)
        fig = Figure(key, label, value, unit, tuple(basis), note)
        self.figures[key] = fig
        return fig

    def value(self, key: str) -> Decimal | None:
        return self.figures[key].value


def _accrued(h: Holding, s: Settings, as_of: date) -> Decimal:
    if not s.accrue_open_profit:
        return ZERO
    if h.state is State.ACTIVE:
        elapsed = max(0, (as_of - h.start).days)
        return h.expected_net * min(Decimal(1), Decimal(elapsed) / Decimal(h.contract_days))
    if h.state is State.DELAYED:
        return max(ZERO, h.expected_net - h.realised_net) * (1 - s.provision_delay)
    return ZERO


def _capital_days_held(h: Holding, as_of: date) -> int:
    if h.channel == "awaed":
        return max(0, (min(h.maturity, as_of) - h.start).days)
    end = h.paid_on if h.paid_on else as_of
    return (end - h.start).days


def _xirr(flows, s: Settings) -> Decimal | None:
    try:
        return xirr(flows, s.days_per_year)
    except NoSolution:
        return None


def nav_parts(hs: list[Holding], contribs: list[Contribution], d: date, s: Settings,
              channel: str | None = None) -> dict[str, Decimal]:
    """NAV on a past date, rebuilt from the ledger and the positions.

    contributed + profit paid by `d` + profit accrued on positions open at `d`
    − provision on positions past maturity and now defaulted or delayed. Cash
    in the wallet is the difference between what came in and what is placed,
    so it needs no separate term. Fees are paid from the bank, not the wallet."""
    hs = [h for h in hs if channel is None or h.channel == channel]
    contributed = sum((x.amount for x in contribs if x.date <= d and (channel is None or x.channel == channel)), ZERO)
    realised = accrued = provision = ZERO
    for h in hs:
        if h.start > d:
            continue
        if h.paid_on is not None and h.paid_on <= d:
            realised += h.realised_net
            continue
        if h.channel == "awaed" and h.maturity <= d:
            realised += h.realised_net
            continue
        if h.state is State.DEFAULTED and h.maturity < d:
            provision -= h.outstanding * s.provision_default
        elif h.state is State.DELAYED and h.maturity < d:
            provision -= h.outstanding * s.provision_delay
            if s.accrue_open_profit:
                accrued += max(ZERO, h.expected_net - h.realised_net) * (1 - s.provision_delay)
        elif s.accrue_open_profit and h.state is not State.DEFAULTED:
            elapsed = max(0, (d - h.start).days)
            accrued += h.expected_net * min(Decimal(1), Decimal(elapsed) / Decimal(h.contract_days))
    return {"contributed": contributed, "realised": realised, "accrued": accrued, "provision": provision,
            "nav": contributed + realised + accrued + provision}


def _units(r: Report, hs: list[Holding], contribs: list[Contribution], s: Settings, as_of: date) -> None:
    """Unitise the portfolio: each contribution buys units at the NAV per unit
    of the day before it arrived, so later money does not share in earlier
    gains or losses. Value per owner = units × today's NAV per unit."""
    units: dict[str, Decimal] = defaultdict(lambda: ZERO)
    total_units = ZERO
    issues = []
    for d in sorted({x.date for x in contribs}):
        price = Decimal(1) if total_units == 0 else nav_parts(hs, contribs, d - timedelta(1), s)["nav"] / total_units
        for x in (x for x in contribs if x.date == d):
            u = x.amount / price
            units[x.owner] += u
            total_units += u
            issues.append({"date": d, "owner": x.owner, "amount": x.amount, "price": price, "units": u})
    nav = r.channels["total"].nav
    price_now = _ratio(nav, total_units) or ZERO
    rows = []
    for owner in sorted(units):
        mine = [x for x in contribs if x.owner == owner]
        value = units[owner] * price_now
        paid = sum((x.amount for x in mine), ZERO)
        fees = sum((x.bank_fee for x in mine), ZERO)
        flows = [(x.date, -(x.amount + x.bank_fee)) for x in mine] + [(as_of, value)]
        rows.append({"owner": owner, "contributed": paid, "fees": fees, "units": units[owner],
                     "share": _ratio(units[owner], total_units), "value": value, "gain": value - paid - fees,
                     "xirr": _xirr(flows, s)})
    r.tables["units"] = rows
    r.tables["unit_issues"] = issues
    r.add("units.nav_per_unit", "NAV per unit", price_now, "x", ("total.nav",),
          "Units issued at 1.00 on the first contribution, then at the NAV per unit of the day before")


def compute(
    *,
    as_of: date,
    holdings: list[Holding],
    ledger: list[Contribution],
    wallets: dict[str, Decimal],
    wallet_refs: dict[str, tuple[str, ...]],
    settings: Settings,
) -> Report:
    s = settings
    Y = Decimal(s.days_per_year)
    contribs = [c for c in ledger if c.date <= as_of]
    by = {c: [h for h in holdings if h.channel == c] for c in CHANNELS}

    ch: dict[str, ChannelNumbers] = {}
    rounding_topup = ZERO
    # Provision released if the expected recovery on each defaulted position comes in.
    expected_back = {c: sum((h.outstanding * s.provision_default * s.recovery_for(h.ident).expected
                             for h in by[c] if h.state is State.DEFAULTED), ZERO) for c in CHANNELS}
    expected_back["total"] = expected_back["awaed"] + expected_back["manafa"]
    weights = {c: sum((x.amount for x in contribs if x.channel == c), ZERO) for c in CHANNELS}
    target_for = {c: s.expected_return[c][0] for c in CHANNELS}
    target_for["total"] = (_ratio(sum((weights[c] * target_for[c] for c in CHANNELS), ZERO), sum(weights.values(), ZERO))
                           or s.target_low)
    for c in CHANNELS:
        n = ChannelNumbers()
        n.contributed = sum((x.amount for x in contribs if x.channel == c), ZERO)
        n.fees = sum((x.bank_fee for x in contribs if x.channel == c), ZERO)
        for h in by[c]:
            n.realised += h.realised_net
            if h.state is State.ACTIVE:
                n.active += h.principal
            elif h.state is State.DELAYED:
                n.delayed += h.outstanding
                n.provision -= h.outstanding * s.provision_delay
            elif h.state is State.DEFAULTED:
                n.defaulted += h.outstanding
                n.provision -= h.outstanding * s.provision_default
            n.recovered += h.principal_recovered
            n.accrued += _accrued(h, s, as_of)
        n.wallet = wallets.get(c, ZERO)
        if c == "awaed" and -s.awaed_rounding_tolerance <= n.wallet < 0:
            rounding_topup = -n.wallet
            n.wallet = ZERO
        n.wallet_estimate = n.contributed + n.realised - n.active - n.delayed - n.defaulted
        ch[c] = n
    total = ch["awaed"] + ch["manafa"]
    r = Report(as_of, holdings, {**ch, "total": total})

    idents = {c: tuple(h.ident for h in by[c]) for c in CHANNELS}
    idents["total"] = idents["awaed"] + idents["manafa"]
    lrefs = {c: tuple(x.ref for x in contribs if x.channel == c) for c in CHANNELS}
    lrefs["total"] = lrefs["awaed"] + lrefs["manafa"]
    wrefs = {c: wallet_refs.get(c, ()) for c in CHANNELS}
    wrefs["total"] = wrefs["awaed"] + wrefs["manafa"]

    # --- A. Returns by channel -------------------------------------------------
    for c, n in r.channels.items():
        closed = [h for h in holdings if h.state is State.CLOSED and (c == "total" or h.channel == c)]
        opened = [h for h in holdings if h.state is not State.CLOSED and (c == "total" or h.channel == c)]
        r.add(f"{c}.contributed", "Capital contributed", n.contributed, "SAR", lrefs[c])
        r.add(f"{c}.fees", "Transfer fees paid", n.fees, "SAR", lrefs[c])
        r.add(f"{c}.realised", "Realised net profit", n.realised, "SAR", [h.ident for h in closed],
              "Awaed at maturity; Manafa on the statement's payment date")
        r.add(f"{c}.active", "Principal – performing", n.active, "SAR",
              [h.ident for h in opened if h.state is State.ACTIVE])
        r.add(f"{c}.delayed", "Principal – delayed (net of recoveries)", n.delayed, "SAR",
              [h.ident for h in opened if h.state is State.DELAYED])
        r.add(f"{c}.defaulted", "Principal – defaulted (net of recoveries)", n.defaulted, "SAR",
              [h.ident for h in opened if h.state is State.DEFAULTED])
        r.add(f"{c}.provision", "Less: credit-loss provision", n.provision, "SAR",
              [h.ident for h in opened if h.state in (State.DELAYED, State.DEFAULTED)],
              f"{s.provision_default:.0%} of defaulted and {s.provision_delay:.0%} of delayed principal")
        r.add(f"{c}.recovered", "Principal recovered on open troubled notes", n.recovered, "SAR",
              [h.ident for h in opened if h.principal_recovered])
        r.add(f"{c}.accrued", "Accrued income on open positions", n.accrued, "SAR", [h.ident for h in opened],
              "Net profit × share of actual entry-to-maturity days elapsed")
        r.add(f"{c}.wallet", "Wallet cash (uninvested)", n.wallet, "SAR", wrefs[c],
              "Manafa: statement cash of the mandate; Awaed: derived from the confirmations")
        r.add(f"{c}.nav", "Current NAV", n.nav, "SAR", (f"{c}.active", f"{c}.delayed", f"{c}.defaulted",
              f"{c}.provision", f"{c}.accrued", f"{c}.wallet"))
        r.add(f"{c}.net_gain", "Net gain", n.net_gain, "SAR", (f"{c}.nav", f"{c}.contributed", f"{c}.fees"))
        r.add(f"{c}.roc", "Return on capital (not annualised)", _ratio(n.net_gain, n.contributed), "ratio",
              (f"{c}.net_gain", f"{c}.contributed"))
        flows = [(x.date, -(x.amount + x.bank_fee)) for x in contribs if c == "total" or x.channel == c]
        r.add(f"{c}.xirr", "Money-weighted return – XIRR (annualised)", _xirr(flows + [(as_of, n.nav)], s),
              "ratio", lrefs[c] + (f"{c}.nav",), "Contributions and bank fees on their dates, NAV on the as-of date")
        r.add(f"{c}.nav_recovery", "NAV – recovery view (0% loss on defaults)", n.nav_recovery, "SAR",
              (f"{c}.nav", f"{c}.provision"))
        r.add(f"{c}.xirr_recovery", "XIRR – recovery view", _xirr(flows + [(as_of, n.nav_recovery)], s), "ratio",
              lrefs[c] + (f"{c}.nav_recovery",), "Upside only: assumes every defaulted riyal comes back")
        r.add(f"{c}.nav_expected", "NAV – expected recovery", n.nav + expected_back[c], "SAR",
              (f"{c}.nav", f"{c}.provision"), "Provision released by the expected recovery entered per position")
        r.add(f"{c}.xirr_expected", "XIRR – expected recovery", _xirr(flows + [(as_of, n.nav + expected_back[c])], s),
              "ratio", lrefs[c] + (f"{c}.nav_expected",))

        cd_tenor = sum((h.principal * (h.contract_days if h.channel == "awaed" else (h.paid_on - h.start).days)
                        for h in closed), ZERO)
        r.add(f"{c}.cw_yield", "Capital-weighted yield on deployed capital (closed)",
              _ratio(sum((h.realised_net for h in closed), ZERO) * Y, cd_tenor), "ratio", [h.ident for h in closed],
              "Closed net profit ÷ Σ(principal × days) × 365; Awaed contract days, Manafa days held")
        deal = [h.realised_net / h.principal * Y / h.contract_days for h in closed]
        r.add(f"{c}.avg_deal_yield", "Average deal yield (equal-weighted, closed)",
              sum(deal, ZERO) / len(deal) if deal else None, "ratio", [h.ident for h in closed],
              "Each deal's net return annualised over its entry-to-maturity days")

        held = sum((h.principal * _capital_days_held(h, as_of) for h in holdings if c == "total" or h.channel == c), ZERO)
        mine = [x for x in contribs if c == "total" or x.channel == c]
        avail = sum((x.amount * (as_of - x.date).days for x in mine), ZERO)
        r.add(f"{c}.utilisation", "Capital utilisation (capital-days deployed ÷ capital-days available)",
              _ratio(held, avail), "ratio", (f"{c}.contributed",),
              "Each contribution counts from the day it arrived")
        if c != "total":
            first = min((x.date for x in mine), default=as_of)
            r.add(f"{c}.utilisation_excel", "Capital utilisation – Excel method",
                  _ratio(held, Decimal((as_of - first).days) * n.contributed), "ratio", (f"{c}.contributed",),
                  "Excel treats all capital as present from the first contribution")
        r.add(f"{c}.idle", "Idle cash as % of NAV", _ratio(n.wallet, n.nav), "ratio", (f"{c}.wallet", f"{c}.nav"))
        x = r.value(f"{c}.xirr")
        r.add(f"{c}.target", "Target return (low end)", target_for[c], "ratio",
              ("config/mandate.toml", "awaed.contributed", "manafa.contributed") if c == "total" else ("config/mandate.toml",),
              "Capital-weighted blend of the channel targets" if c == "total" else "Low end of the channel's expected range")
        r.add(f"{c}.vs_target", "Excess return vs target (low end)", None if x is None else x - target_for[c], "ratio",
              (f"{c}.xirr", f"{c}.target"))
        r.add(f"{c}.vs_benchmark", "Excess return vs benchmark (3M SAIBOR)",
              None if x is None else x - s.benchmark_saibor, "ratio", (f"{c}.xirr",), s.benchmark_warning(as_of))

    # Total utilisation, Excel method (each channel from its first contribution)
    num = sum((h.principal * _capital_days_held(h, as_of) for h in holdings), ZERO)
    den = ZERO
    for c in CHANNELS:
        mine = [x for x in contribs if x.channel == c]
        if mine:
            den += Decimal((as_of - min(x.date for x in mine)).days) * ch[c].contributed
    r.add("total.utilisation_excel", "Capital utilisation – Excel method", _ratio(num, den), "ratio",
          ("total.contributed",), "Excel treats all capital as present from the first contribution")

    if rounding_topup:
        r.notes.append(f"Awaed derived wallet was {rounding_topup:,.2f} SAR below zero: Awaed rounds rolled-over "
                       f"principal up to the whole riyal, so this is treated as a rounding top-up and the wallet shown as 0.00")
    _credit(r, by["manafa"], s, as_of)
    _ratings(r, holdings, s)
    _allocation(r, s, contribs)
    _income(r, holdings, contribs, as_of, s)
    _ladder(r, holdings, as_of, s)
    _sensitivity(r, s)
    _forecast(r, holdings, s)
    _policy(r, s)
    _monthly(r, holdings, contribs, as_of, s)
    _units(r, holdings, contribs, s, as_of)
    _evidence(r, contribs, s, as_of)

    for c in CHANNELS:
        n = ch[c]
        diff = n.wallet - n.wallet_estimate
        r.checks.append(
            f"{c.title()} wallet: statement-based {n.wallet:,.2f} vs estimate (contributed + realised − "
            f"outstanding) {n.wallet_estimate:,.2f}: " + ("agree" if abs(diff) < Decimal("0.005") else f"differ by {diff:,.2f}")
        )
    return r


def _credit(r: Report, hs: list[Holding], s: Settings, as_of: date) -> None:
    closed = [h for h in hs if h.state is State.CLOSED]
    troubled = [h for h in hs if h.state in (State.DELAYED, State.DEFAULTED)]
    oids = {h.oid for h in hs}
    defaulted_oids = {h.oid for h in hs if h.state is State.DEFAULTED}
    m = r.channels["manafa"]
    total_nav = r.channels["total"].nav
    r.add("credit.positions", "Positions funded (rows; a split counts twice)", len(hs), "count", [h.ident for h in hs])
    r.add("credit.opportunities", "Opportunities funded (IDs)", len(oids), "count", sorted(oids))
    for st in State:
        r.add(f"credit.{st.value}", f"{st.value.title()} positions", sum(1 for h in hs if h.state is st), "count",
              [h.ident for h in hs if h.state is st])
    r.add("credit.default_rate_count", "Default rate – by count (one per opportunity ID)",
          _ratio(Decimal(len(defaulted_oids)), Decimal(len(oids))), "ratio", sorted(defaulted_oids))
    r.add("credit.default_rate_principal", "Default rate – by principal funded",
          _ratio(sum((h.principal for h in hs if h.state is State.DEFAULTED), ZERO), sum((h.principal for h in hs), ZERO)),
          "ratio", [h.ident for h in hs if h.state is State.DEFAULTED])
    r.add("credit.recovered_share", "Recovered so far on delayed and defaulted principal",
          _ratio(sum((h.principal_recovered for h in troubled), ZERO), sum((h.principal for h in troubled), ZERO)),
          "ratio", [h.ident for h in troubled], "Over delayed and defaulted principal (the Excel used delayed only)")
    trouble = m.delayed + m.defaulted
    r.add("credit.troubled_manafa_nav", "Troubled principal % of Manafa NAV", _ratio(trouble, m.nav), "ratio",
          ("manafa.delayed", "manafa.defaulted", "manafa.nav"))
    r.add("credit.troubled_total_nav", "Troubled exposure % of total NAV (policy basis)", _ratio(trouble, total_nav),
          "ratio", ("manafa.delayed", "manafa.defaulted", "total.nav"), "Before provision")
    late = [(h.paid_on - h.maturity).days for h in closed]
    paid_late = [d for d in late if d > 0]
    r.add("credit.avg_days_late", "Average days late – closed positions paid late",
          Decimal(sum(paid_late)) / len(paid_late) if paid_late else ZERO, "days", [h.ident for h in closed])
    r.add("credit.on_time_share", "% of closed paid on time or early",
          _ratio(Decimal(sum(1 for d in late if d <= 0)), Decimal(len(late))), "ratio", [h.ident for h in closed])
    gross = sum((h.gross for h in closed), ZERO)
    cost = -sum((h.fee + h.vat for h in closed), ZERO)
    r.add("credit.fee_drag", "Fee drag (fee + VAT ÷ gross profit, paid)", _ratio(cost, gross), "ratio",
          [h.ident for h in closed], "From the statement's repayment rows; fee rebates reduce the fee")
    r.add("credit.loss_cover", "Loss cover: realised profit ÷ defaulted principal", _ratio(m.realised, m.defaulted), "x",
          ("manafa.realised", "manafa.defaulted"))
    be = None if m.defaulted == 0 else max(ZERO, 1 - (m.net_gain + m.defaulted * s.provision_default) / m.defaulted)
    r.add("credit.break_even", "Break-even recovery on defaults", be, "ratio", ("manafa.net_gain", "manafa.defaulted"))
    overdue = [h for h in hs if h.state is not State.CLOSED and h.maturity < as_of]
    rows = []
    for h in overdue:
        rec = s.recovery_for(h.ident)
        late = (as_of - h.maturity).days
        # writeoff_days = 0 disables the automatic write-off: only a manual
        # "written off" status writes a position off.
        written_off = rec.status == "written off" or (
            s.writeoff_days > 0 and h.state is State.DEFAULTED and late > s.writeoff_days
            and h.principal_recovered == 0)
        rows.append({"position": h.ident, "status": h.state.value, "rating": h.rating, "principal": h.principal,
                     "recovered": h.principal_recovered, "outstanding": h.outstanding, "maturity": h.maturity,
                     "days_past_maturity": late, "collection": rec.status, "expected_recovery": rec.expected,
                     "last_action": rec.last_action, "last_action_note": rec.last_action_note,
                     "written_off": written_off})
        if written_off and rec.status != "written off":
            r.notes.append(f"{h.ident} is {late} days past maturity with nothing recovered: past the "
                           f"{s.writeoff_days}-day write-off point. Mark it written off or record the collection action")
        if h.state is State.DEFAULTED and rec.last_action is None:
            r.notes.append(f"{h.ident}: no collection action recorded. Add the last action and its date in settings")
    r.tables["troubled"] = rows
    funded = sum((h.principal for h in hs), ZERO)
    lost = sum((h.outstanding * (1 - s.recovery_for(h.ident).expected) for h in hs if h.state is State.DEFAULTED), ZERO)
    r.add("credit.observed_loss", "Observed loss rate (defaulted principal net of expected recovery ÷ funded)",
          _ratio(lost, funded), "ratio", [h.ident for h in hs if h.state is State.DEFAULTED],
          "Used as the loss per Manafa cycle in the forecast unless overridden in settings")


def _ratings(r: Report, hs: list[Holding], s: Settings) -> None:
    Y = Decimal(s.days_per_year)
    groups: dict[str, list[Holding]] = defaultdict(list)
    for h in hs:
        if h.channel == "manafa":  # Awaed deposits are not rated
            groups[h.rating].append(h)
    outstanding_all = sum((h.outstanding for g in groups.values() for h in g), ZERO)
    rows = []
    for k in sorted(groups):
        g = groups[k]
        closed = [h for h in g if h.state is State.CLOSED]
        out = sum((h.outstanding for h in g), ZERO)
        cd = sum((h.principal * (h.paid_on - h.start).days for h in closed), ZERO)
        realised = sum((h.realised_net for h in closed), ZERO)
        defaulted_principal = sum((h.outstanding for h in g if h.state is State.DEFAULTED), ZERO)
        provision = -sum((h.outstanding * (s.provision_default if h.state is State.DEFAULTED else s.provision_delay)
                          for h in g if h.state in (State.DEFAULTED, State.DELAYED)), ZERO)
        net = realised + provision
        late = [(h.paid_on - h.maturity).days for h in closed]
        paid_late = [d for d in late if d > 0]
        rows.append({
            "rating": k, "positions": len(g), "outstanding": out, "share": _ratio(out, outstanding_all),
            "realised": realised, "cw_yield": _ratio(realised * Y, cd),
            "defaulted": sum(1 for h in g if h.state is State.DEFAULTED),
            "defaulted_principal": defaulted_principal, "provision": provision, "net_after_loss": net,
            "net_yield": _ratio(net * Y, cd),
            "on_time_share": _ratio(Decimal(sum(1 for d in late if d <= 0)), Decimal(len(late))),
            "avg_days_late": Decimal(sum(paid_late)) / len(paid_late) if paid_late else ZERO,
        })
    r.tables["ratings"] = rows


def _allocation(r: Report, s: Settings, contribs: list[Contribution]) -> None:
    nav = r.channels["total"].nav
    rows = []
    ch = r.channels
    # Invested principal per sleeve; wallet cash of both platforms is its own sleeve,
    # so the sleeves add up to total NAV.
    invested = {c: ch[c].nav - ch[c].wallet for c in CHANNELS}
    invested["cash"] = ch["total"].wallet
    for c, label in (("awaed", "Time deposits – Awaed"), ("manafa", "Crowdfunded financing – Manafa"),
                     ("cash", "Cash (platform wallets)"), ("tier3", "Higher yield / equities")):
        actual = invested.get(c, ZERO)
        target = s.allocation.get(c, ZERO)
        rows.append({"sleeve": label, "target_share": target, "target_sar": target * nav, "actual_sar": actual,
                     "actual_share": _ratio(actual, nav), "rebalance": target * nav - actual})
    r.tables["allocation"] = rows
    owners = []
    total = sum((x.amount for x in contribs), ZERO)
    for o, budget in s.budget.items():
        got = sum((x.amount for x in contribs if x.owner == o), ZERO)
        owners.append({"owner": o, "budget": budget, "contributed": got, "difference": got - budget,
                       "share": _ratio(got, total)})
    r.tables["capital_by_owner"] = owners


MIN_ANNUALISE_DAYS = 30


def _income(r: Report, hs: list[Holding], contribs: list[Contribution], as_of: date, s: Settings) -> None:
    """Income earned in each window on an accrual basis: profit paid in the
    window plus the change in accrued profit, so a window is not inflated by a
    deal that happened to pay inside it. Annualised over the days the
    portfolio actually existed in the window, on its average NAV."""
    first = min((x.date for x in contribs), default=as_of)
    rows = []
    for label, start in (("Last 7 days", as_of - timedelta(7)), ("Last 30 days", as_of - timedelta(30)),
                         ("Last 90 days", as_of - timedelta(90)),
                         ("Year to date", date(as_of.year, 1, 1) - timedelta(1)),
                         ("Last 365 days", as_of - timedelta(365))):
        start = max(start, first - timedelta(1))
        days = (as_of - start).days
        got = {}
        for c in CHANNELS:
            a, b = nav_parts(hs, contribs, start, s, c), nav_parts(hs, contribs, as_of, s, c)
            got[c] = b["realised"] - a["realised"] + b["accrued"] - a["accrued"]
        tot = got["awaed"] + got["manafa"]
        avg_nav = (nav_parts(hs, contribs, start, s)["nav"] + r.channels["total"].nav) / 2
        ok = days >= MIN_ANNUALISE_DAYS
        rows.append({"period": label, "days": days, "awaed": got["awaed"], "manafa": got["manafa"], "total": tot,
                     "annualised_on_nav": _ratio(tot * s.days_per_year, avg_nav * days) if ok else None,
                     "note": "" if ok else f"Not annualised: under {MIN_ANNUALISE_DAYS} days"})
    r.tables["income_by_period"] = rows


BUCKETS = (("0–7 days", 0, 7), ("8–30 days", 8, 30), ("31–60 days", 31, 60), ("61–90 days", 61, 90), ("Over 90 days", 91, 10**6))


def _ladder(r: Report, hs: list[Holding], as_of: date, s: Settings) -> None:
    ch = r.channels
    rows = [{"bucket": "Available now (wallet cash)", "awaed": ch["awaed"].wallet, "manafa": ch["manafa"].wallet,
             "profit": ZERO}]
    active = [h for h in hs if h.state is State.ACTIVE]
    for label, lo, hi in BUCKETS:
        sel = [h for h in active if lo <= max(0, (h.maturity - as_of).days) <= hi]
        rows.append({"bucket": label,
                     "awaed": sum((h.principal for h in sel if h.channel == "awaed"), ZERO),
                     "manafa": sum((h.principal for h in sel if h.channel == "manafa"), ZERO),
                     "profit": sum((h.expected_net for h in sel), ZERO)})
    troubled = [h for h in hs if h.state in (State.DELAYED, State.DEFAULTED)]
    carrying = sum((h.outstanding * (1 - (s.provision_default if h.state is State.DEFAULTED else s.provision_delay))
                    for h in troubled), ZERO)
    rows.append({"bucket": "Overdue (delayed + defaulted, net of provision)", "awaed": ZERO, "manafa": carrying,
                 "profit": sum((_accrued(h, s, as_of) for h in troubled), ZERO), "timing": "uncertain"})
    cum = ZERO
    for row in rows:
        row["total"] = row["awaed"] + row["manafa"] + row["profit"]
        cum += row["total"]
        row["cumulative"] = cum
    r.tables["liquidity"] = rows
    due = sum((row["total"] for row in rows[1:3]), ZERO)
    r.add("liquidity.due_30", f"Due in the next {s.upcoming_days} days", due, "SAR",
          [h.ident for h in active if (h.maturity - as_of).days <= s.upcoming_days])


def _sensitivity(r: Report, s: Settings) -> None:
    m, t = r.channels["manafa"], r.channels["total"]
    other_gain = t.net_gain - m.net_gain
    rows = []
    for loss in (Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1")):
        nav = m.nav + m.defaulted * s.provision_default - m.defaulted * loss
        gain = nav - m.contributed - m.fees
        rows.append({"loss_on_defaulted": loss, "manafa_nav": nav, "manafa_gain": gain,
                     "manafa_roc": _ratio(gain, m.contributed), "total_roc": _ratio(gain + other_gain, t.contributed)})
    r.tables["sensitivity"] = rows


def _forecast(r: Report, hs: list[Holding], s: Settings) -> None:
    Y = Decimal(s.days_per_year)
    loss = s.forecast_loss_per_cycle
    loss_basis = "set in settings"
    if loss is None:
        loss = r.value("credit.observed_loss") or ZERO
        loss_basis = "observed"
    r.add("forecast.loss_per_cycle", "Credit loss per Manafa cycle used in the forecast", loss, "ratio",
          ("credit.observed_loss",), f"{loss_basis.title()}: defaulted principal net of expected recovery ÷ funded"
          if loss_basis == "observed" else "Override set in settings")
    compound = lambda per, n: Decimal(repr((1 + float(per)) ** float(n) - 1)) if per > -1 else Decimal(-1)  # noqa: E731
    out = {}
    for c in CHANNELS:
        closed = [h for h in hs if h.channel == c and h.state is State.CLOSED]
        y = r.value(f"{c}.cw_yield")
        if not closed or y is None:
            out[c] = None
            continue
        days = [Decimal(h.contract_days if c == "awaed" else (h.paid_on - h.start).days) for h in closed]
        cycle = sum(days, ZERO) / len(days)
        cycles = Y / cycle
        gross_per = y * cycle / Y
        per = gross_per - (loss if c == "manafa" else ZERO)
        gross_annual, annual = compound(gross_per, cycles), compound(per, cycles)
        base = r.channels[c].contributed
        out[c] = {"yield": y, "cycle_days": cycle, "cycles": cycles, "gross_per_cycle": gross_per,
                  "loss_per_cycle": loss if c == "manafa" else ZERO, "per_cycle": per, "gross_annual": gross_annual,
                  "expected_loss": gross_annual - annual, "annual": annual, "base": base, "profit": base * annual}
        r.add(f"forecast.{c}", f"Forecast annual return – {c.title()} (net of expected loss)", annual, "ratio",
              (f"{c}.cw_yield", "forecast.loss_per_cycle"),
              "Closed-deal yield compounded over a year of cycles, fully reinvested, less expected credit loss")
        r.add(f"forecast.{c}_gross", f"Forecast annual return – {c.title()} (before losses)", gross_annual, "ratio",
              (f"{c}.cw_yield",))
    if out["awaed"] and out["manafa"]:
        profit = out["awaed"]["profit"] + out["manafa"]["profit"]
        base = out["awaed"]["base"] + out["manafa"]["base"]
        r.add("forecast.total", "Forecast annual return – blended (net of expected loss)", _ratio(profit, base), "ratio",
              ("forecast.awaed", "forecast.manafa"),
              f"Assumes full deployment and {loss:.2%} credit loss per Manafa cycle ({loss_basis})")
        a, m = out["awaed"]["annual"], out["manafa"]["annual"]
        target = s.target_low
        if a >= target:
            need, note = ZERO, "Awaed alone reaches the target"
        elif m <= a or (target - a) / (m - a) > 1:
            need, note = None, "Not reachable at the current loss rate: more Manafa lowers the blended return"
        else:
            need, note = (target - a) / (m - a), ""
        r.add("forecast.manafa_share_needed", "Manafa share needed to reach the target (low end)", need, "ratio",
              ("forecast.awaed", "forecast.manafa"), note)
    r.tables["forecast"] = [dict(channel=c, **v) for c, v in out.items() if v]


def _band(x: Decimal | None, lo: Decimal, hi: Decimal) -> str:
    if x is None:
        return "n/a"
    return "Below expected" if x < lo else "Above expected" if x > hi else "Within range"


def _policy(r: Report, s: Settings) -> None:
    t, m = r.channels["total"], r.channels["manafa"]
    rows = []
    for c in CHANNELS:
        lo, hi = s.expected_return[c]
        x = r.value(f"{c}.xirr")
        rows.append({"check": f"{c.title()} XIRR vs expected {lo:.1%}–{hi:.1%}", "actual": x,
                     "limit": f"{lo:.1%}–{hi:.1%}", "status": _band(x, lo, hi)})
    for row in r.tables["allocation"]:
        rows.append({"check": f"Allocation – {row['sleeve']}", "actual": row["actual_share"],
                     "limit": f"target {row['target_share']:.0%}", "status": "info"})
    carrying = [h.outstanding * (1 - s.provision_default if h.state is State.DEFAULTED else 1 - s.provision_delay)
                if h.state in (State.DEFAULTED, State.DELAYED) else h.outstanding
                for h in r.holdings if h.channel == "manafa" and h.state is not State.CLOSED]
    single = _ratio(max(carrying, default=ZERO), m.nav)
    liq = sum((row["total"] for row in r.tables["liquidity"][:3]), ZERO)
    # Awaed money that matured within the grace period is in transit to the next
    # deposit, not idle.
    in_transit = min(r.channels["awaed"].wallet, sum(
        (h.principal + h.realised_net for h in r.holdings
         if h.channel == "awaed" and h.maturity <= r.as_of and (r.as_of - h.maturity).days <= s.idle_grace_days), ZERO))
    in_transit = max(ZERO, in_transit)
    idle = _ratio(t.wallet - in_transit, t.nav)
    r.add("total.in_transit", f"Awaed cash matured in the last {s.idle_grace_days} days", in_transit, "SAR",
          ("awaed.wallet",), "Excluded from the idle-cash check while it waits to be redeployed")
    r.add("manafa.idle_policy", "Manafa wallet cash (% of Manafa NAV)", _ratio(m.wallet, m.nav), "ratio",
          ("manafa.wallet", "manafa.nav"))
    top_rating = max(r.tables["ratings"], key=lambda row: row["share"] or ZERO, default=None)
    limits = [
        ("Max single Manafa position (% of Manafa NAV)", single, "max_single_manafa_position", "max", ("manafa.nav",)),
        ("Max troubled exposure (% of total NAV, before provision)", r.value("credit.troubled_total_nav"),
         "max_troubled_exposure", "max", ("credit.troubled_total_nav",)),
        (f"Max idle cash (% of total NAV, excl. money matured ≤{s.idle_grace_days} days)", idle, "max_idle_cash", "max",
         ("total.wallet", "total.in_transit", "total.nav")),
        ("Min liquidity: cash + maturing ≤30 days (% of NAV)", _ratio(liq, t.nav), "min_liquidity", "min",
         ("total.wallet", "liquidity.due_30", "total.nav")),
    ]
    if top_rating and "max_single_rating_share" in s.limits:
        limits.append((f"Max share of Manafa outstanding in one rating (largest: {top_rating['rating']})",
                       top_rating["share"], "max_single_rating_share", "max", ("manafa.outstanding",)))
    cap = s.limits["max_single_manafa_position"] * m.nav
    r.add("policy.max_new_manafa_position", "Largest new Manafa position allowed", cap, "SAR", ("manafa.nav",),
          f"{s.limits['max_single_manafa_position']:.0%} of Manafa NAV; check before funding")
    rows.append({"check": "Pre-trade: largest new Manafa position allowed (SAR)", "actual": cap,
                 "limit": f"{s.limits['max_single_manafa_position']:.0%} of Manafa NAV", "status": "info", "unit": "SAR"})
    for label, actual, key, kind, basis in limits:
        lim = s.limits[key]
        ok = actual is not None and (actual <= lim if kind == "max" else actual >= lim)
        rows.append({"check": label, "actual": actual, "limit": f"{'≤' if kind == 'max' else '≥'} {lim:.0%}",
                     "status": "Within limit" if ok else "BREACH"})
        r.add(f"policy.{key}", label, actual, "ratio", basis, note=f"limit {'≤' if kind == 'max' else '≥'} {lim:.0%}")
    x = r.value("total.xirr")
    verdict = "n/a" if x is None else ("On target" if x >= r.value("total.target") else
                                       "Below target, above benchmark" if x >= s.benchmark_saibor else "Below benchmark")
    r.tables["policy"] = rows
    r.notes.append(f"Verdict on the headline XIRR: {verdict}")


def _evidence(r: Report, contribs: list[Contribution], s: Settings, as_of: date) -> None:
    missing = [x for x in contribs if not x.evidenced]
    if missing:
        r.notes.append(f"{len(missing)} of {len(contribs)} capital-ledger contributions have no bank reference or "
                       f"source document yet; add bank_ref and evidence in config/capital_ledger.toml")
    r.tables["ledger"] = [{"date": x.date, "channel": x.channel, "owner": x.owner, "amount": x.amount,
                           "bank_fee": x.bank_fee, "bank_ref": x.bank_ref, "evidence": x.evidence,
                           "evidenced": x.evidenced} for x in contribs]
    w = s.benchmark_warning(as_of)
    if w:
        r.notes.append(w)


def _month_end_navs(r: Report, hs: list[Holding], contribs: list[Contribution], as_of: date, s: Settings) -> None:
    """NAV on the last calendar day of each month (as-of for the current month)."""
    for row in r.tables["monthly"]:
        m = row["month"]
        end = (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(1)
        if end >= as_of:
            row["month_end"], row["nav_end"], row["month_to_date"] = as_of, r.channels["total"].nav, True
        else:
            row["month_end"], row["nav_end"], row["month_to_date"] = end, nav_parts(hs, contribs, end, s)["nav"], False
    rebuilt = nav_parts(hs, contribs, as_of, s)["nav"]
    diff = r.channels["total"].nav - rebuilt
    # Month-end NAVs before the as-of date use the rebuilt figure, so it must tie out today.
    r.checks.append(f"Ledger-rebuilt NAV on {as_of:%d %b %Y}: {rebuilt:,.2f} vs reported {r.channels['total'].nav:,.2f}: "
                    + ("agree" if abs(diff) < Decimal("1") else f"differ by {diff:,.2f}"))


def _monthly(r: Report, hs: list[Holding], contribs: list[Contribution], as_of: date, s: Settings) -> None:
    _monthly_rows(r, hs, contribs, as_of)
    _month_end_navs(r, hs, contribs, as_of, s)


def _monthly_rows(r: Report, hs: list[Holding], contribs: list[Contribution], as_of: date) -> None:
    first = min([x.date for x in contribs] + [h.start for h in hs], default=as_of).replace(day=1)
    months = []
    m = first
    while m <= as_of:
        months.append(m)
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    rows, cum = [], ZERO
    for m in months:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        inm = lambda d: d is not None and m <= d < nxt and d <= as_of  # noqa: E731
        aw = sum((h.realised_net for h in hs if h.channel == "awaed" and inm(h.paid_on)), ZERO)
        mf = sum((h.realised_net for h in hs if h.channel == "manafa" and inm(h.paid_on)), ZERO)
        cum += aw + mf
        rows.append({
            "month": m, "contributed": sum((x.amount for x in contribs if m <= x.date < nxt), ZERO),
            "awaed_realised": aw, "manafa_realised": mf, "total_realised": aw + mf, "cumulative_realised": cum,
            "expected_open": sum((h.expected_net for h in hs if h.state is State.ACTIVE and m <= h.maturity < nxt), ZERO),
            "new_manafa": sum((h.principal for h in hs if h.channel == "manafa" and m <= h.start < nxt), ZERO),
            "principal_maturing": sum((h.principal for h in hs if h.state is State.ACTIVE and m <= h.maturity < nxt), ZERO),
            "defaulted_by_due_month": sum((h.principal for h in hs if h.state is State.DEFAULTED and m <= h.maturity < nxt), ZERO),
        })
    r.tables["monthly"] = rows
