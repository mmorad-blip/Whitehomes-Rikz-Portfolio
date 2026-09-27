"""Turn a stored snapshot into what the dashboard template shows."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

from markupsafe import Markup

from . import charts


def D(v) -> Decimal | None:
    return None if v is None else Decimal(v)


LADDER_LABELS = ["Now", "0–7", "8–30", "31–60", "61–90", ">90", "Overdue"]
CHANGE_GROUPS = (
    ("Statements received", ("statement", "first")),
    ("Report date", ("as_of",)),
    ("Positions", ("position", "status")),
    ("Figures", ("figure",)),
    ("Policy checks", ("policy",)),
)


def _group_changes(changes: list[dict]) -> list[tuple[str, list[dict]]]:
    out = []
    for title, types in CHANGE_GROUPS:
        items = [c for c in changes if c.get("type") in types]
        if items:
            out.append((title, items))
    return out


def _gap_summary(gaps: list[str]) -> list[str]:
    """One line per kind of Awaed chain note, for the PDF and page top."""
    out = []
    kinds: dict[str, list[str]] = {}
    for g in gaps:
        m = re.match(r"Awaed \[(\w+)\] (.*)", g)
        if m:
            kinds.setdefault(m.group(1), []).append(m.group(2))
    if "rounding" in kinds:
        amts = [Decimal(x) for x in (re.search(r"needs ([\d.,]+) SAR", t).group(1).replace(",", "") for t in kinds["rounding"])]
        out.append(f"Awaed rolled principal up to the whole riyal {len(amts)} times; the derived wallet dipped by "
                   f"{min(amts):.2f}–{max(amts):.2f} SAR (rounding, shown not corrected).")
    if "idle" in kinds:
        days = [int(re.search(r"\((\d+) days later\)", t).group(1)) for t in kinds["idle"]]
        out.append(f"Awaed money waited {min(days)}–{max(days)} days between a maturity and the next deposit on "
                   f"{len(days)} occasions ({sum(days)} idle days in total).")
    for k in ("shortfall", "uninvested"):
        out.extend(kinds.get(k, []))
    return out


KPI_SPECS = (
    # key, label, unit, which direction is good, policy check it is held against
    ("total.nav", "NAV", "SAR", "up", None),
    ("total.realised", "Realised profit", "SAR", "up", None),
    ("total.xirr", "XIRR – accounting view", "ratio", "up", None),
    ("total.xirr_recovery", "XIRR – recovery view", "ratio", "up", None),
    ("total.idle", "Idle cash", "ratio", "down", "Max idle cash"),
    ("credit.troubled_total_nav", "Troubled exposure", "ratio", "down", "Max troubled exposure"),
)
SPARK_SLOT = {"total.nav": 1, "total.realised": 1, "total.xirr": 3, "total.xirr_recovery": 3,
              "total.idle": 2, "credit.troubled_total_nav": 4}


def _k(v: Decimal) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:,.2f}m"
    if a >= 1_000:
        return f"{v / 1_000:,.1f}k"
    return f"{v:,.0f}"


def _limit(text: str) -> Decimal | None:
    m = re.search(r"([\d.]+)\s*%", text or "")
    return Decimal(m.group(1)) / 100 if m else None


def _delta(new, old, unit: str, better: str) -> dict | None:
    if new is None or old is None:
        return None
    d = new - old
    tiny = Decimal("0.005") if unit == "SAR" else Decimal("0.00005")
    if abs(d) < tiny:
        return {"text": "no change", "cls": "flat", "arrow": "–"}
    arrow = "▲" if d > 0 else "▼"
    text = f"{abs(d):,.2f}" if unit == "SAR" else f"{abs(d) * 100:.2f} pts"
    good = (d > 0) == (better == "up")
    return {"text": text, "cls": "up" if good else "down", "arrow": arrow}


def auto_headline(v, prev: dict | None) -> str:
    """One line in plain words from the figures; admin may replace it."""
    nav, real, contrib = v("total.nav"), v("total.realised"), v("total.contributed")
    if nav is None:
        return "Portfolio report"
    if prev and prev.get("total.nav") is not None:
        dn = nav - D(prev["total.nav"])
        dr = (real or 0) - D(prev.get("total.realised") or 0)
        lead = f"Realised profit up SAR {dr:,.0f}" if dr >= 1 else "No new realised profit"
        if abs(dn) < 1:
            return f"{lead}, with NAV unchanged at SAR {_k(nav)}"
        verb = "rises" if dn > 0 else "slips"
        return f"{lead} {'as' if dr >= 1 else 'while'} NAV {verb} to SAR {_k(nav)}"
    if contrib:
        return f"NAV of SAR {_k(nav)} on SAR {_k(contrib)} contributed"
    return f"NAV of SAR {_k(nav)}"


def _summary(v, breaches: list[dict]) -> Markup:
    nav, contrib, real = v("total.nav"), v("total.contributed"), v("total.realised")
    parts = []
    if nav is not None and contrib:
        gain = nav - contrib
        parts.append(Markup("NAV of <strong>SAR {}</strong> against SAR {} contributed, a net {} of SAR {} ({:+.2f}%).")
                     .format(f"{nav:,.2f}", f"{contrib:,.2f}", "gain" if gain >= 0 else "loss", f"{abs(gain):,.2f}",
                             gain / contrib * 100))
    if real is not None:
        parts.append(Markup("Realised profit stands at <strong>SAR {}</strong>.").format(f"{real:,.2f}"))
    x, xr = v("total.xirr"), v("total.xirr_recovery")
    if x is not None and xr is not None:
        parts.append(Markup("The money-weighted return is <strong>{:.2f}%</strong> a year on the accounting view and "
                            "{:.2f}% if defaulted principal is recovered in full.").format(x * 100, xr * 100))
    idle, troubled = v("total.idle"), v("credit.troubled_total_nav")
    if idle is not None and troubled is not None:
        parts.append(Markup("Idle cash is {:.1f}% of NAV and troubled exposure {:.1f}%.").format(idle * 100, troubled * 100))
    if breaches:
        names = ", ".join(re.sub(r"\s*\(.*\)", "", b["check"]).lower() for b in breaches)
        parts.append(Markup("{} policy {} breached: {}.").format(
            len(breaches), "limit is" if len(breaches) == 1 else "limits are", names))
    else:
        parts.append(Markup("Every policy limit is met."))
    return Markup(" ").join(parts)


def build(snapshot, versions: list, pdf: bool = False, headline_override: str | None = None) -> dict:
    doc = json.loads(snapshot.report)
    figs = {k: {**f, "value": D(f["value"]), "excel": D(f.get("excel"))} for k, f in doc["figures"].items()}
    t = doc["tables"]

    def v(key):
        return figs[key]["value"] if key in figs else None

    history = sorted((ver, head or {}) for ver, _, _, head in versions if ver <= snapshot.version)
    prev = history[-2][1] if len(history) > 1 else None
    prev_version = history[-2][0] if len(history) > 1 else None
    policy = t.get("policy", [])
    kpis = []
    for key, label, unit, better, check in KPI_SPECS:
        val = v(key)
        row = next((r for r in policy if r["check"].startswith(check)), None) if check else None
        bar = None
        if row and val is not None:
            lim = _limit(row["limit"])
            if lim:
                ratio = val / lim
                bar = {"fill": float(min(ratio, 1)) * 100, "cls": "bad" if row["status"] == "BREACH" else "ok",
                       "left": f"{ratio * 100:.0f}% of limit", "right": f"limit {row['limit']}"}
        badge = None
        if key == "total.nav" and v("total.roc") is not None:
            roc = v("total.roc")
            badge = (f"{roc * 100:+.1f}% on capital", "ok" if roc >= 0 else "bad")
        elif key == "total.realised" and v("total.cw_yield") is not None:
            badge = (f"{v('total.cw_yield') * 100:.1f}% yield, closed", "info")
        elif key == "total.xirr" and v("total.vs_target") is not None:
            vt = v("total.vs_target")
            badge = (f"{vt * 100:+.1f} pts vs target", "ok" if vt >= 0 else "bad")
        elif key == "total.xirr_recovery":
            badge = ("0% loss on defaults", "info")
        elif row:
            badge = (row["status"], "bad" if row["status"] == "BREACH" else "ok")
        trend = [D(h.get(key)) for _, h in history]
        kpis.append({
            "key": key, "label": label, "value": val, "unit": unit, "badge": badge, "bar": bar,
            "delta": _delta(val, D(prev.get(key)) if prev and prev.get(key) is not None else None, unit, better),
            "spark": charts.sparkline(trend, slot=SPARK_SLOT.get(key, 1), literal=pdf),
        })

    breaches = [r for r in policy if r["status"] == "BREACH"]
    below = [r for r in policy if r["status"] == "Below expected"]
    liquid = next((r for r in policy if r["check"].startswith("Min liquidity")), None)
    owners = t.get("capital_by_owner", [])
    open_n = {s: int(v(f"credit.{s}") or 0) for s in ("active", "delayed", "defaulted")}
    cov = snapshot.coverage
    facts = [
        ("Capital in", _k(v("total.contributed") or Decimal(0)),
         " · ".join(f"{o['owner']} {_k(D(o['contributed']))}" for o in owners)),
        ("Open positions", str(sum(open_n.values())),
         " · ".join(f"{n} {s}" for s, n in open_n.items() if n) or "none open"),
        ("Liquid within 30 days", f"{D(liquid['actual']) * 100:.1f}%" if liquid else "–",
         f"of NAV · limit {liquid['limit']}" if liquid else ""),
        ("Manafa statement to", date.fromisoformat(cov["manafa"]["statement_to"]).strftime("%d %b %Y"),
         f"{cov['awaed']['confirmations']} Awaed confirmations"),
    ]
    hero = {
        "headline": headline_override or auto_headline(v, prev),
        "auto_headline": auto_headline(v, prev),
        "overridden": bool(headline_override),
        "summary": _summary(v, breaches),
        "badge": ((f"{len(breaches)} policy {'breach' if len(breaches) == 1 else 'breaches'}", "bad") if breaches
                  else (f"{len(below)} below expected", "warn") if below else ("Within policy", "ok")),
        "facts": facts,
        "prev_version": prev_version,
    }
    comp = [
        ("Performing principal", v("total.active")),
        ("Delayed / defaulted, net of provision",
         (v("total.delayed") or 0) + (v("total.defaulted") or 0) + (v("total.provision") or 0)),
        ("Accrued income", v("total.accrued")),
        ("Wallet cash", v("total.wallet")),
    ]
    nav_total = sum((val for _, val in comp if val and val > 0), Decimal(0))
    monthly = t.get("monthly", [])
    months = [date.fromisoformat(r["month"]).strftime("%b") for r in monthly]
    ladder = t.get("liquidity", [])
    excel_rows = [(k, f) for k, f in figs.items() if f["excel"] is not None]
    steps = [("Contributed", v("total.contributed")), ("Realised", v("total.realised")),
             ("Accrued", v("total.accrued")), ("Provision", v("total.provision"))]
    steps = [(n, val) for n, val in steps if val is not None]
    other = (v("total.nav") or 0) - sum((val for _, val in steps), Decimal(0))
    if abs(other) >= Decimal("0.5"):
        steps.append(("Other", other))
    ratings = t.get("ratings", [])
    return {
        "hero": hero,
        "snapshot": snapshot,
        "doc": doc,
        "figs": figs,
        "tables": t,
        "kpis": kpis,
        "versions": versions,
        "coverage": snapshot.coverage,
        "changes": snapshot.changes,
        "excel_rows": excel_rows,
        "charts": {
            "nav": charts.share_bar(comp, title="NAV composition", literal=pdf),
            "monthly": charts.bars(months, [("Awaed", [D(r["awaed_realised"]) for r in monthly]),
                                            ("Manafa", [D(r["manafa_realised"]) for r in monthly])],
                                   stacked=True, title="Realised profit by month", literal=pdf),
            "ladder": charts.bars(LADDER_LABELS[:len(ladder)],
                                  [("Awaed", [D(r["awaed"]) for r in ladder]), ("Manafa", [D(r["manafa"]) for r in ladder]),
                                   ("Profit", [D(r["profit"]) for r in ladder])], stacked=True,
                                  title="Liquidity schedule", literal=pdf, slot_width=64),
            "allocation": charts.bars(["Awaed", "Manafa", "Tier 3"][:len(t.get("allocation", []))],
                                      [("Target", [D(r["target_sar"]) for r in t.get("allocation", [])]),
                                       ("Actual", [D(r["actual_sar"]) for r in t.get("allocation", [])])],
                                      title="Allocation vs policy", literal=pdf),
            "waterfall": charts.waterfall(steps, "NAV", title="How capital became NAV", literal=pdf) if steps else "",
            "ratings": charts.hbars([(f"{r['rating']} · {r['positions']} pos", D(r["outstanding"])) for r in ratings],
                                    slot=2, title="Outstanding exposure by rating", literal=pdf),
        },
        "legends": {
            "nav": [(n, c, val, (val / nav_total) if nav_total and val else None)
                    for (n, c), (_, val) in zip(charts.legend([p[0] for p in comp], pdf), comp)],
            "channels": charts.legend(["Awaed", "Manafa"], pdf),
            "ladder": charts.legend(["Awaed", "Manafa", "Profit"], pdf),
            "allocation": charts.legend(["Target", "Actual"], pdf),
        },
        "change_groups": _group_changes(snapshot.changes),
        "notes_main": [n for n in doc["notes"] if not n.startswith("Awaed [")],
        "awaed_gaps": [n for n in doc["notes"] if n.startswith("Awaed [")],
        "awaed_gap_summary": _gap_summary([n for n in doc["notes"] if n.startswith("Awaed [")]),
        "channels": ("awaed", "manafa", "total"),
        "returns_rows": ["contributed", "fees", "realised", "active", "delayed", "defaulted", "provision", "accrued",
                         "wallet", "nav", "net_gain", "roc", "xirr", "nav_recovery", "xirr_recovery", "cw_yield",
                         "avg_deal_yield", "utilisation", "utilisation_excel", "idle", "vs_target", "vs_benchmark"],
        "credit_keys": [k for k in figs if k.startswith("credit.")],
    }
