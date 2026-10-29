"""Turn a stored snapshot into what the dashboard template shows."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

from markupsafe import Markup

from . import charts, periods


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


def _delta_pct(new, old) -> dict | None:
    """Change of a SAR flow against the previous period, as a percentage."""
    if new is None or old is None:
        return None
    d = new - old
    if abs(d) < Decimal("0.005"):
        return {"text": "no change", "cls": "flat", "arrow": "–"}
    arrow, cls = ("▲", "up") if d > 0 else ("▼", "down")
    text = f"{abs(d) / abs(old) * 100:.1f}%" if old else f"SAR {abs(d):,.0f}"
    return {"text": text, "cls": cls, "arrow": arrow}


def _delta_count(new: int, old: int) -> dict:
    if new == old:
        return {"text": "no change", "cls": "flat", "arrow": "–"}
    return {"text": str(abs(new - old)), "cls": "flat", "arrow": "▲" if new > old else "▼"}


def auto_headline(v, cur: dict, prv: dict, label: str, prev_label: str) -> str:
    """One line in plain words from the period figures; admin may replace it."""
    nav = v("total.nav")
    if nav is None:
        return "Portfolio report"
    real, old = cur["realised"], prv["realised"] if prv else Decimal(0)
    if real < 1:
        lead = f"No profit realised in {label}"
    elif old >= 1:
        change = (real - old) / old * 100
        word = "up" if change >= 0 else "down"
        lead = f"SAR {_k(real)} realised in {label}, {word} {abs(change):.0f}% on {prev_label}"
    else:
        lead = f"SAR {_k(real)} realised in {label}"
    return f"{lead}, with NAV at SAR {_k(nav)}"


def _period_sentence(cur: dict, prv: dict | None, label: str, prev_label: str) -> Markup:
    y, py = cur["yield"], prv["yield"] if prv else None
    text = Markup("In {}, <strong>SAR {}</strong> of profit was received and SAR {} earned on an accrual basis").format(
        label, f"{cur['realised']:,.2f}", f"{cur['earned']:,.2f}")
    if prv:
        text += Markup(" (SAR {} and SAR {} in {})").format(f"{prv['realised']:,.2f}", f"{prv['earned']:,.2f}",
                                                             prev_label)
    text += Markup(".")
    if y is not None:
        text += Markup(" Deployed capital averaged SAR {} and yielded <strong>{:.2f}%</strong> a year{}.").format(
            f"{cur['avg_capital']:,.0f}", y * 100,
            Markup(", against {:.2f}%").format(py * 100) if py is not None else "")
    return text


def _summary(v, breaches: list[dict]) -> Markup:
    nav, contrib, real = v("total.nav"), v("total.contributed"), v("total.realised")
    parts = []
    gain = v("total.net_gain")
    if gain is None and nav is not None and contrib:
        gain = nav - contrib
    if nav is not None and contrib and gain is not None:
        parts.append(Markup("NAV of <strong>SAR {}</strong> against SAR {} contributed, a net {} of SAR {} ({:+.2f}%) "
                            "after fees.")
                     .format(f"{nav:,.2f}", f"{contrib:,.2f}", "gain" if gain >= 0 else "loss", f"{abs(gain):,.2f}",
                             gain / contrib * 100))
    if real is not None:
        parts.append(Markup("Realised profit stands at <strong>SAR {}</strong>.").format(f"{real:,.2f}"))
    x = v("total.xirr")
    if x is not None:
        parts.append(Markup("The money-weighted return (XIRR) is <strong>{:.2f}%</strong> a year.").format(x * 100))
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


def saibor_fact(as_of: date) -> dict | None:
    """The 3M SAIBOR benchmark as currently set in config/settings.toml."""
    from ..config import load_settings
    from ..errors import Rejected

    try:
        s = load_settings()
    except (Rejected, KeyError, ValueError, ArithmeticError):
        return None
    if s.benchmark_is_placeholder:
        sub, status = "Placeholder, not yet entered from the official source", "warn"
    else:
        parts = [f"as of {s.benchmark_as_of:%d %b %Y}" if s.benchmark_as_of else "no as-of date",
                 s.benchmark_source]
        sub = " · ".join(p for p in parts if p)
        status = "warn" if s.benchmark_warning(as_of) else "ok"
    return {"value": f"{s.benchmark_saibor * 100:.2f}%", "sub": sub, "status": status}


def build(snapshot, versions: list, pdf: bool = False, headline_override: str | None = None,
          period: str = periods.DEFAULT) -> dict:
    doc = json.loads(snapshot.report)
    figs = {k: {**f, "value": D(f["value"]), "excel": D(f.get("excel"))} for k, f in doc["figures"].items()}
    t = doc["tables"]

    def v(key):
        return figs[key]["value"] if key in figs else None

    if period not in dict(periods.PERIODS):
        period = periods.DEFAULT
    as_of = snapshot.as_of
    holdings = doc.get("holdings", [])
    cur_w, prev_w = periods.windows(period, as_of)
    monthly = t.get("monthly", [])
    cur = periods.with_capital(periods.measure(holdings, as_of, cur_w), monthly, cur_w)
    # Before the first position there is nothing to compare against.
    prv = (periods.with_capital(periods.measure(holdings, as_of, prev_w), monthly, prev_w)
           if periods.covered(holdings, prev_w) else None)
    series = periods.monthly_series(holdings, as_of, monthly=monthly)
    start_point = periods.point_at(versions, snapshot.version, cur_w.start)
    start_head = start_point[2] if start_point else {}
    history = sorted((a, ver, head or {}) for ver, a, _, head in versions if ver <= snapshot.version and a is not None)
    new_capital = cur["injected"]
    vs_prev = f"vs {prev_w.label()}"
    vs_start = f"vs {start_point[1]:%d %b %Y}" if start_point else None

    policy = t.get("policy", [])

    def limit_bar(check, val):
        row = next((r for r in policy if r["check"].startswith(check)), None)
        if not row or val is None:
            return None, None
        lim = _limit(row["limit"])
        bar = None
        if lim:
            ratio = val / lim
            bar = {"fill": float(min(ratio, 1)) * 100, "cls": "bad" if row["status"] == "BREACH" else "ok",
                   "left": f"{ratio * 100:.0f}% of limit", "right": f"limit {row['limit']}"}
        return bar, (row["status"], "bad" if row["status"] == "BREACH" else "ok")

    def point_kpi(key, label, unit, better, check=None, badge=None, sub=None):
        val = v(key)
        bar, status = limit_bar(check, val) if check else (None, None)
        old = D(start_head.get(key)) if start_head.get(key) is not None else None
        trend = [D(h.get(key)) for _, _, h in history]
        return {"key": key, "label": label, "value": val, "unit": unit, "badge": badge or status, "bar": bar,
                "vs": vs_start, "delta": _delta(val, old, unit, better), "sub": sub,
                "spark": charts.sparkline(trend, slot=SPARK_SLOT.get(key, 1), literal=pdf)}

    def flow_kpi(key, label, unit, badge=None, sub=None, slot=1):
        val, old = cur[key], prv[key] if prv else None
        delta = _delta_pct(val, old) if unit == "SAR" else _delta(val, old, unit, "up")
        if prv is None:
            sub = sub or "No earlier period to compare"
        return {"key": None, "label": label, "value": val, "unit": unit, "badge": badge, "bar": None,
                "vs": vs_prev, "delta": delta, "sub": sub,
                "spark": charts.sparkline([m[key] for m in series], slot=slot, literal=pdf)}

    roc = v("total.roc")
    nav_sub = f"SAR {new_capital:,.0f} new capital this period" if new_capital else None
    kpis = [
        point_kpi("total.nav", "NAV", "SAR", "up", sub=nav_sub,
                  badge=(f"{roc * 100:+.1f}% on capital", "ok" if roc >= 0 else "bad") if roc is not None else None),
        flow_kpi("realised", "Realised profit", "SAR",
                 badge=(f"{cur['closed']} closed", "info") if cur["closed"] else None),
        flow_kpi("yield", "Annualised yield", "ratio", slot=3,
                 badge=(f"on SAR {_k(cur['avg_capital'])} avg. capital", "info") if cur["avg_capital"] else None),
        flow_kpi("injected", "New capital injected", "SAR", slot=2,
                 badge=("No new money", "info") if not cur["injected"] else None,
                 sub=(f"SAR {_k(cur['deployed'])} placed in {cur['opened']} positions, "
                      f"SAR {_k(cur['reinvested'])} of it reinvested") if cur["opened"] else None),
        point_kpi("total.idle", "Idle cash", "ratio", "down", check="Max idle cash"),
        point_kpi("credit.troubled_total_nav", "Troubled exposure", "ratio", "down", check="Max troubled exposure"),
    ]

    def row(label, key, unit):
        a, b = cur[key], prv[key] if prv else None
        if b is None:
            delta = None
        elif unit == "n":
            delta = _delta_count(a, b)
        else:
            delta = _delta_pct(a, b) if unit == "SAR" else _delta(a, b, unit, "up")
        return {"label": label, "cur": a, "prev": b, "unit": unit, "delta": delta}

    comparison = {
        "cur": cur_w.label(), "prev": prev_w.label(),
        "rows": [
            row("Realised profit (cash received)", "realised", "SAR"),
            row("Income earned (accrual basis)", "earned", "SAR"),
            row("Annualised yield on deployed capital", "yield", "ratio"),
            row("Average capital at work", "avg_capital", "SAR"),
            row("New capital injected", "injected", "SAR"),
            row("Gross placements (incl. rollovers)", "deployed", "SAR"),
            row("of which reinvested from returns", "reinvested", "SAR"),
            row("Principal returned", "closed_principal", "SAR"),
            row("Positions opened", "opened", "n"),
            row("Positions closed", "closed", "n"),
        ],
    }
    period_ctx = {
        "kind": period, "options": periods.PERIODS, "label": cur_w.label(), "prev_label": prev_w.label(),
        "partial": prv is not None and cur_w.end == as_of and as_of != periods._month_end(as_of),
        "no_prior": prv is None,
        "inception": periods.inception(holdings),
    }

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
        ("XIRR since inception", f"{v('total.xirr') * 100:.2f}%" if v("total.xirr") is not None else "–",
         "Accounting view, net of provisions"),
        ("Manafa statement to", date.fromisoformat(cov["manafa"]["statement_to"]).strftime("%d %b %Y"),
         f"{cov['awaed']['confirmations']} Awaed confirmations"),
    ]
    benchmark = saibor_fact(as_of)
    auto = auto_headline(v, cur, prv, cur_w.label(), prev_w.label())
    hero = {
        "headline": headline_override or auto,
        "auto_headline": auto,
        "overridden": bool(headline_override),
        "summary": _period_sentence(cur, prv, cur_w.label(), prev_w.label()) + Markup(" ") + _summary(v, breaches),
        "badge": ((f"{len(breaches)} policy {'breach' if len(breaches) == 1 else 'breaches'}", "bad") if breaches
                  else (f"{len(below)} below expected", "warn") if below else ("Within policy", "ok")),
        "facts": facts,
        "benchmark": benchmark,
        "liquid": liquid,
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
             ("Accrued", v("total.accrued")), ("Provision", v("total.provision")),
             ("Fees", -abs(v("total.fees")) if v("total.fees") else None)]
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
        "period": period_ctx,
        "comparison": comparison,
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
            "ratings": charts.hbars([(f"{r['rating']} · {r['positions']} ever funded", D(r["outstanding"]))
                                     for r in ratings if "not rated" not in str(r["rating"]).lower()],
                                    slot=2, title="Manafa outstanding exposure by rating", literal=pdf),
        },
        "legends": {
            "nav": [(n, c, val, (val / nav_total) if nav_total and val else None)
                    for (n, c), (_, val) in zip(charts.legend([p[0] for p in comp], pdf), comp)],
            "channels": charts.legend(["Awaed", "Manafa"], pdf),
            "ladder": charts.legend(["Awaed", "Manafa", "Profit"], pdf),
            "allocation": charts.legend(["Target", "Actual"], pdf),
        },
        "change_groups": _group_changes(snapshot.changes),
        "config_changes": _config_changes(),
        "notes_main": [n for n in doc["notes"] if not n.startswith("Awaed [")],
        "awaed_gaps": [n for n in doc["notes"] if n.startswith("Awaed [")],
        "awaed_gap_summary": _gap_summary([n for n in doc["notes"] if n.startswith("Awaed [")]),
        "channels": ("awaed", "manafa", "total"),
        "returns_rows": ["contributed", "fees", "realised", "active", "delayed", "defaulted", "provision", "accrued",
                         "wallet", "nav", "net_gain", "roc", "xirr", "xirr_expected", "cw_yield",
                         "avg_deal_yield", "utilisation", "idle", "vs_target", "vs_benchmark"],
        "credit_keys": [k for k in figs if k.startswith("credit.")],
    }


def _config_changes():
    from rikz.config import load_change_log
    try:
        return load_change_log()
    except Exception:  # a malformed log must not take the report down
        return []


def _ended(h: dict):
    return periods._d(h.get("paid_on")) or periods._d(h["maturity"])


def monthly_reports(snapshot, versions: list) -> list[dict]:
    """One report per calendar month since inception, newest first.

    Flows come from the selected report's positions, so later corrections
    flow back into earlier months; the month's own report version is kept so
    readers can open the figures exactly as they were published.
    """
    doc = json.loads(snapshot.report)
    holdings = doc.get("holdings", [])
    monthly = doc.get("tables", {}).get("monthly", [])
    as_of = snapshot.as_of
    first = periods.inception(holdings)
    if first is None:
        return []
    out = []
    m = as_of.replace(day=1)
    while m >= first.replace(day=1):
        end = periods._month_end(m)
        complete = end <= as_of
        if complete:
            w = periods.Window(m, end)
            pm = periods.shift(m, -1)
            pw = periods.Window(pm, periods._month_end(pm))
        else:
            w, pw = periods.windows("month", as_of)
        cur = periods.with_capital(periods.measure(holdings, as_of, w), monthly, w)
        prv = (periods.with_capital(periods.measure(holdings, as_of, pw), monthly, pw)
               if periods.covered(holdings, pw) else None)
        published = [(a, ver, head or {}) for ver, a, _, head in versions
                     if ver <= snapshot.version and a is not None and m <= periods._d(a) <= end]
        last = max(published, key=lambda x: (x[0], x[1])) if published else None
        nav = last[2].get("total.nav") if last else None

        def row(label, key, unit, cur=cur, prv=prv):
            a, b = cur[key], prv[key] if prv else None
            if b is None:
                delta = None
            elif unit == "n":
                delta = _delta_count(a, b)
            elif unit == "SAR":
                delta = _delta_pct(a, b)
            else:
                delta = _delta(a, b, unit, "up")
            return {"label": label, "cur": a, "prev": b, "unit": unit, "delta": delta}

        out.append({
            "key": f"{m:%Y-%m}",
            "label": f"{m:%B %Y}",
            "short": f"{m:%b %Y}",
            "window": w,
            "prev_label": pw.label(),
            "complete": complete,
            "cur": cur,
            "prev": prv,
            "nav": Decimal(str(nav)) if nav is not None else None,
            "version": last[1] if last else None,
            "version_as_of": last[0] if last else None,
            "rows": [
                row("Profit received", "realised", "SAR"),
                row("Profit earned (accrual)", "earned", "SAR"),
                row("Annualised yield on capital", "yield", "ratio"),
                row("Average capital at work", "avg_capital", "SAR"),
                row("New capital injected", "injected", "SAR"),
                row("Gross placements (incl. rollovers)", "deployed", "SAR"),
                row("of which reinvested from returns", "reinvested", "SAR"),
                row("Principal returned", "closed_principal", "SAR"),
                row("Positions opened", "opened", "n"),
                row("Positions closed", "closed", "n"),
            ],
            "opened": sorted((h for h in holdings if w.start <= periods._d(h["start"]) <= w.end),
                             key=lambda h: h["start"]),
            "closed": sorted((h for h in holdings if h["state"] == "closed" and w.start <= _ended(h) <= w.end),
                             key=_ended),
        })
        m = periods.shift(m, -1)
    return out
