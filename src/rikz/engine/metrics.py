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
              lrefs[c] + (f"{c}.nav_recovery",))

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
        r.add(f"{c}.vs_target", "Excess return vs target (low end)", None if x is None else x - s.target_low, "ratio",
              (f"{c}.xirr",))
        r.add(f"{c}.vs_benchmark", "Excess return vs benchmark (3M SAIBOR)",
              None if x is None else x - s.benchmark_saibor, "ratio", (f"{c}.xirr",),
              "Benchmark is a placeholder until updated in settings" if s.benchmark_is_placeholder else "")

    # Total utilisation, Excel method (each channel from its first contribution)
    num = sum((h.principal * _capital_days_held(h, as_of) for h in holdings), ZERO)
    den = ZERO
    for c in CHANNELS:
        mine = [x for x in contribs if x.channel == c]
        if mine:
            den += Decimal((as_of - min(x.date for x in mine)).days) * ch[c].contributed
    r.add("total.utilisation_excel", "Capital utilisation – Excel method", _ratio(num, den), "ratio",
          ("total.contributed",), "Excel treats all capital as present from the first contribution")

    _credit(r, by["manafa"], s, as_of)
    _ratings(r, holdings, s)
    _allocation(r, s, contribs)
    _income(r, holdings, as_of, s)
    _ladder(r, holdings, as_of, s)
    _sensitivity(r, s)
    _forecast(r, holdings, s)
    _policy(r, s)
    _monthly(r, holdings, contribs, as_of)

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
    r.tables["troubled"] = [
        {"position": h.ident, "status": h.state.value, "rating": h.rating, "principal": h.principal,
         "recovered": h.principal_recovered, "outstanding": h.outstanding, "maturity": h.maturity,
         "days_past_maturity": (as_of - h.maturity).days}
        for h in overdue
    ]


def _ratings(r: Report, hs: list[Holding], s: Settings) -> None:
    Y = Decimal(s.days_per_year)
    groups: dict[str, list[Holding]] = defaultdict(list)
    for h in hs:
        groups[h.rating if h.channel == "manafa" else "Awaed (not rated)"].append(h)
    outstanding_all = sum((h.outstanding for h in hs), ZERO)
    rows = []
    for k in sorted(groups, key=lambda k: (k.startswith("Awaed"), k)):
        g = groups[k]
        closed = [h for h in g if h.state is State.CLOSED]
        out = sum((h.outstanding for h in g), ZERO)
        cd = sum((h.principal * (h.contract_days if h.channel == "awaed" else (h.paid_on - h.start).days) for h in closed), ZERO)
        rows.append({
            "rating": k, "positions": len(g), "outstanding": out, "share": _ratio(out, outstanding_all),
            "realised": sum((h.realised_net for h in closed), ZERO),
            "cw_yield": _ratio(sum((h.realised_net for h in closed), ZERO) * Y, cd),
            "defaulted": sum(1 for h in g if h.state is State.DEFAULTED),
        })
    r.tables["ratings"] = rows


def _allocation(r: Report, s: Settings, contribs: list[Contribution]) -> None:
    nav = r.channels["total"].nav
    rows = []
    for c, label in (("awaed", "Time deposit – Awaed"), ("manafa", "Sukuk – Manafa"), ("tier3", "Higher yield / equities")):
        actual = r.channels[c].nav if c in r.channels else ZERO
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


def _income(r: Report, hs: list[Holding], as_of: date, s: Settings) -> None:
    nav = r.channels["total"].nav
    rows = []
    ytd_days = (as_of - date(as_of.year, 1, 1)).days + 1
    for label, days, start in (("Last 7 days", 7, as_of - timedelta(7)), ("Last 30 days", 30, as_of - timedelta(30)),
                               ("Last 90 days", 90, as_of - timedelta(90)),
                               ("Year to date", ytd_days, date(as_of.year, 1, 1) - timedelta(1)),
                               ("Last 365 days", 365, as_of - timedelta(365))):
        got = {c: sum((h.realised_net for h in hs if h.channel == c and h.paid_on and start < h.paid_on <= as_of), ZERO)
               for c in CHANNELS}
        tot = got["awaed"] + got["manafa"]
        rows.append({"period": label, "awaed": got["awaed"], "manafa": got["manafa"], "total": tot,
                     "annualised_on_nav": _ratio(tot * s.days_per_year, nav * days)})
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
        per = y * cycle / Y - (s.forecast_loss_per_cycle if c == "manafa" else ZERO)
        annual = Decimal(repr((1 + float(per)) ** float(cycles) - 1))
        base = r.channels[c].contributed
        out[c] = {"yield": y, "cycle_days": cycle, "cycles": cycles, "per_cycle": per, "annual": annual,
                  "base": base, "profit": base * annual}
        r.add(f"forecast.{c}", f"Forecast annual return – {c.title()}", annual, "ratio", (f"{c}.cw_yield",),
              "Historical capital-weighted yield compounded over a year of cycles, fully reinvested")
    if out["awaed"] and out["manafa"]:
        profit = out["awaed"]["profit"] + out["manafa"]["profit"]
        base = out["awaed"]["base"] + out["manafa"]["base"]
        blended = _ratio(profit, base)
        r.add("forecast.total", "Forecast annual return – blended", blended, "ratio", ("forecast.awaed", "forecast.manafa"),
              f"Assumes full deployment and {s.forecast_loss_per_cycle:.0%} credit loss per Manafa cycle")
        a, m = out["awaed"]["annual"], out["manafa"]["annual"]
        need = None if m == a else min(Decimal(1), max(ZERO, (s.target_low - a) / (m - a)))
        r.add("forecast.manafa_share_needed", "Manafa share needed to reach the target (low end)", need, "ratio",
              ("forecast.awaed", "forecast.manafa"), "100% means the target is out of reach at current yields")
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
    limits = (
        ("Max single Manafa position (% of Manafa NAV)", single, "max_single_manafa_position", "max", ("manafa.nav",)),
        ("Max troubled exposure (% of total NAV, before provision)", r.value("credit.troubled_total_nav"),
         "max_troubled_exposure", "max", ("credit.troubled_total_nav",)),
        ("Max idle cash (% of total NAV)", r.value("total.idle"), "max_idle_cash", "max", ("total.idle",)),
        ("Min liquidity: cash + maturing ≤30 days (% of NAV)", _ratio(liq, t.nav), "min_liquidity", "min",
         ("total.wallet", "liquidity.due_30", "total.nav")),
    )
    for label, actual, key, kind, basis in limits:
        lim = s.limits[key]
        ok = actual is not None and (actual <= lim if kind == "max" else actual >= lim)
        rows.append({"check": label, "actual": actual, "limit": f"{'≤' if kind == 'max' else '≥'} {lim:.0%}",
                     "status": "Within limit" if ok else "BREACH"})
        r.add(f"policy.{key}", label, actual, "ratio", basis, note=f"limit {'≤' if kind == 'max' else '≥'} {lim:.0%}")
    x = r.value("total.xirr")
    verdict = "n/a" if x is None else ("On target" if x >= s.target_low else
                                       "Below target, above benchmark" if x >= s.benchmark_saibor else "Below benchmark")
    r.tables["policy"] = rows
    r.notes.append(f"Verdict on the headline XIRR: {verdict}")


def _monthly(r: Report, hs: list[Holding], contribs: list[Contribution], as_of: date) -> None:
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
