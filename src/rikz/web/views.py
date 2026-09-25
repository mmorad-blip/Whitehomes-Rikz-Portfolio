"""Turn a stored snapshot into what the dashboard template shows."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

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


def build(snapshot, versions: list, pdf: bool = False) -> dict:
    doc = json.loads(snapshot.report)
    figs = {k: {**f, "value": D(f["value"]), "excel": D(f.get("excel"))} for k, f in doc["figures"].items()}
    t = doc["tables"]

    def v(key):
        return figs[key]["value"] if key in figs else None

    kpis = [
        ("NAV", v("total.nav"), "SAR", "total.nav"),
        ("Realised profit", v("total.realised"), "SAR", "total.realised"),
        ("XIRR – accounting view", v("total.xirr"), "ratio", "total.xirr"),
        ("XIRR – recovery view", v("total.xirr_recovery"), "ratio", "total.xirr_recovery"),
        ("Idle cash", v("total.idle"), "ratio", "total.idle"),
        ("Troubled exposure", v("credit.troubled_total_nav"), "ratio", "credit.troubled_total_nav"),
    ]
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
    return {
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
