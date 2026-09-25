"""Plain-text and JSON renderings of a computed report (the website and PDF
come in phase 4)."""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..config import CONFIG_DIR, Settings
from .metrics import Report

ASSUMPTIONS = (
    "Platform statements are the source of truth; the Excel model is shown only for comparison.",
    "Awaed 'Week Murabaha' = 7 days and '1 Month Murabaha' = 30 days from the order date.",
    "An Awaed deposit matures on its maturity date; from then its principal and return are realised and held "
    "as wallet cash until a newer confirmation shows them rolled over.",
    "Awaed wallet cash is derived from the confirmations and the capital ledger (Awaed issues no account statement).",
    "Manafa figures cover the Whitehomes mandate only (rule in config/mandate.toml); the rest of the account is "
    "shown in the reconciliation line.",
    "Manafa accrual uses each position's actual entry-to-maturity days.",
    "Unpaid profit on a delayed note stays expected until paid; no write-off while guarantees stand.",
    "Split investments are separate positions; the default rate by count counts one per opportunity ID.",
    "Troubled exposure = delayed + defaulted principal before provision ÷ total NAV.",
    "Capital contributions and bank fees come from the manual capital ledger (config/capital_ledger.toml).",
)


def load_excel_reference(path: Path | None = None) -> dict[str, Decimal]:
    p = path or CONFIG_DIR / "excel_reference.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as fh:
        return {k: Decimal(v) for k, v in tomllib.load(fh)["values"].items()}


def assumptions(s: Settings) -> list[str]:
    out = list(ASSUMPTIONS)
    out.append(f"Credit provision: {s.provision_default:.0%} of defaulted and {s.provision_delay:.0%} of delayed "
               "principal (accounting view); the recovery view assumes 0% loss.")
    out.append(f"Forecast credit loss per Manafa cycle: {s.forecast_loss_per_cycle:.0%}.")
    b = f"Benchmark 3M SAIBOR: {s.benchmark_saibor:.2%}"
    out.append(b + (" (placeholder – update in config/settings.toml)." if s.benchmark_is_placeholder else "."))
    return out


def fmt_value(v: Decimal | None, unit: str) -> str:
    if v is None:
        return "–"
    if unit == "SAR":
        return f"{v:,.2f}"
    if unit == "ratio":
        return f"{v * 100:.2f}%"
    if unit == "x":
        return f"{v:.2f}×"
    if unit == "days":
        return f"{v:.1f}"
    return f"{v:,.0f}"


SECTIONS = (
    ("Returns", ("contributed", "fees", "realised", "active", "delayed", "defaulted", "provision", "accrued", "wallet",
                 "nav", "net_gain", "roc", "xirr", "nav_recovery", "xirr_recovery", "cw_yield", "avg_deal_yield",
                 "utilisation", "utilisation_excel", "idle", "vs_target", "vs_benchmark")),
)


def to_text(r: Report, s: Settings, excel: dict[str, Decimal] | None = None) -> str:
    excel = excel or {}
    L: list[str] = [f"Whitehomes capital-market portfolio – as of {r.as_of:%d %b %Y} (SAR)", ""]

    def differs(key: str) -> str | None:
        if key not in excel:
            return None
        f = r.figures[key]
        xv = excel[key]
        same = f.value is not None and abs(f.value - xv) < (Decimal("0.005") if f.unit == "SAR" else Decimal("0.00005"))
        return None if same else fmt_value(xv, f.unit)

    def cmp(key: str) -> str:
        d = differs(key)
        return f"   [Excel {d}]" if d else ""

    L.append(f"{'A. Returns':<58}{'Awaed':>14}{'Manafa':>14}{'Total':>14}")
    for m in SECTIONS[0][1]:
        f = r.figures.get(f"total.{m}") or r.figures[f"manafa.{m}"]
        vals = []
        for c in ("awaed", "manafa", "total"):
            g = r.figures.get(f"{c}.{m}")
            vals.append(fmt_value(g.value, g.unit) if g else "")
        parts = [f"{c.title()} {d}" for c in ("awaed", "manafa", "total") if (d := differs(f"{c}.{m}"))]
        extra = f"   [Excel: {'; '.join(parts)}]" if parts else ""
        L.append(f"  {f.label:<56}{vals[0]:>14}{vals[1]:>14}{vals[2]:>14}{extra}")
    L.append("")
    L.append("B. Credit risk – Manafa (mandate)")
    for k, f in r.figures.items():
        if k.startswith("credit."):
            L.append(f"  {f.label:<66}{fmt_value(f.value, f.unit):>12}{cmp(k)}")
    for t in r.tables["troubled"]:
        L.append(f"  overdue: {t['position']} {t['status']} rating {t['rating']} outstanding {t['outstanding']:,.2f}, "
                 f"{t['days_past_maturity']} days past maturity")
    L.append("")
    L.append("C. Exposure and results by rating")
    for row in r.tables["ratings"]:
        L.append(f"  {row['rating']:<20} positions {row['positions']:>3}  outstanding {row['outstanding']:>12,.2f}  "
                 f"realised {row['realised']:>10,.2f}  yield {fmt_value(row['cw_yield'], 'ratio'):>8}")
    L.append("")
    L.append("D. Allocation vs policy")
    for row in r.tables["allocation"]:
        L.append(f"  {row['sleeve']:<28} target {row['target_share']:.0%}  actual {fmt_value(row['actual_share'], 'ratio'):>8}"
                 f"  NAV {row['actual_sar']:>12,.2f}  rebalance {row['rebalance']:>+12,.2f}")
    for row in r.tables["capital_by_owner"]:
        L.append(f"  of which {row['owner']:<12} contributed {row['contributed']:>12,.2f}  budget {row['budget']:>12,.2f}"
                 f"  share {fmt_value(row['share'], 'ratio')}")
    L.append("")
    L.append("E. Realised income by period")
    for row in r.tables["income_by_period"]:
        L.append(f"  {row['period']:<16} Awaed {row['awaed']:>10,.2f}  Manafa {row['manafa']:>10,.2f}  total "
                 f"{row['total']:>10,.2f}  annualised on NAV {fmt_value(row['annualised_on_nav'], 'ratio')}")
    L.append("")
    L.append("F. Liquidity schedule")
    for row in r.tables["liquidity"]:
        L.append(f"  {row['bucket']:<48} Awaed {row['awaed']:>12,.2f}  Manafa {row['manafa']:>12,.2f}  profit "
                 f"{row['profit']:>8,.2f}  total {row['total']:>12,.2f}  cumulative {row['cumulative']:>12,.2f}")
    L.append("")
    L.append("G. Sensitivity – loss on defaulted principal")
    for row in r.tables["sensitivity"]:
        L.append(f"  loss {row['loss_on_defaulted']:>5.0%}  Manafa NAV {row['manafa_nav']:>12,.2f}  Manafa return "
                 f"{fmt_value(row['manafa_roc'], 'ratio'):>8}  total return {fmt_value(row['total_roc'], 'ratio'):>8}")
    L.append("")
    L.append("H. Forecast – a year fully reinvested at historical yields")
    for row in r.tables["forecast"]:
        L.append(f"  {row['channel'].title():<8} yield {fmt_value(row['yield'], 'ratio')}  cycle {row['cycle_days']:.1f} days"
                 f"  cycles {row['cycles']:.2f}  annual {fmt_value(row['annual'], 'ratio')}  profit {row['profit']:,.2f}")
    for k in ("forecast.total", "forecast.manafa_share_needed"):
        if k in r.figures:
            f = r.figures[k]
            L.append(f"  {f.label:<66}{fmt_value(f.value, f.unit):>12}{cmp(k)}")
    L.append("")
    L.append("I. Policy checks")
    for row in r.tables["policy"]:
        L.append(f"  {row['check']:<62} {fmt_value(row['actual'], 'ratio'):>9}  {row['limit']:<12} {row['status']}")
    L.append("")
    L.append("J. Monthly")
    for row in r.tables["monthly"]:
        L.append(f"  {row['month']:%b %Y}  contributed {row['contributed']:>10,.2f}  realised {row['total_realised']:>9,.2f}"
                 f"  cumulative {row['cumulative_realised']:>9,.2f}  new Manafa {row['new_manafa']:>10,.2f}")
    L.append("")
    L.append("Checks")
    L.extend(f"  {c}" for c in r.checks)
    L.append("")
    L.append("Notes")
    L.extend(f"  {n}" for n in r.notes)
    L.append("")
    L.append("Assumptions")
    L.extend(f"  {i}. {a}" for i, a in enumerate(assumptions(s), 1))
    if excel:
        L.append("")
        L.append("[Excel …] = the hand-kept Excel V2.2 value where it differs from the statement-based figure.")
    return "\n".join(L)


def _json(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def to_json(r: Report, s: Settings, excel: dict[str, Decimal] | None = None) -> str:
    excel = excel or {}
    doc = {
        "as_of": r.as_of,
        "figures": {k: {**asdict(f), "excel": excel.get(k)} for k, f in r.figures.items()},
        "tables": r.tables,
        "holdings": [
            {"ident": h.ident, "channel": h.channel, "state": h.state.value, "rating": h.rating,
             "principal": h.principal, "start": h.start, "maturity": h.maturity, "expected_net": h.expected_net,
             "realised_net": h.realised_net, "paid_on": h.paid_on, "outstanding": h.outstanding, "refs": list(h.refs)}
            for h in r.holdings
        ],
        "checks": r.checks,
        "notes": r.notes,
        "assumptions": assumptions(s),
    }
    return json.dumps(doc, default=_json, ensure_ascii=False, indent=2)
