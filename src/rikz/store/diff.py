"""'Changes since last report': compares two snapshots' reports."""

from __future__ import annotations

from decimal import Decimal

HEADLINE = (
    ("total.nav", "NAV", "SAR"),
    ("total.realised", "Realised profit", "SAR"),
    ("total.accrued", "Accrued income", "SAR"),
    ("total.wallet", "Wallet cash", "SAR"),
    ("total.xirr", "XIRR, accounting view", "ratio"),
    ("total.xirr_recovery", "XIRR, recovery view", "ratio"),
    ("awaed.xirr", "Awaed XIRR", "ratio"),
    ("manafa.xirr", "Manafa XIRR", "ratio"),
    ("total.idle", "Idle cash (% of NAV)", "ratio"),
    ("credit.troubled_total_nav", "Troubled exposure (% of NAV)", "ratio"),
    ("total.contributed", "Capital contributed", "SAR"),
)


def _fmt(v: str | None, unit: str) -> str:
    if v is None:
        return "–"
    d = Decimal(v)
    return f"{d:,.2f}" if unit == "SAR" else f"{d * 100:.2f}%"


def _moved(old: str | None, new: str | None, unit: str) -> bool:
    if old is None or new is None:
        return old != new
    tol = Decimal("0.005") if unit == "SAR" else Decimal("0.00005")
    return abs(Decimal(old) - Decimal(new)) >= tol


def changes(prev: dict | None, new: dict, prev_files: dict[str, str], new_files: dict[str, str]) -> list[dict]:
    """prev/new: report JSON documents; *_files: sha -> file name."""
    out: list[dict] = []
    fresh = [(sha, name) for sha, name in new_files.items() if sha not in prev_files]
    pdfs = [x for x in fresh if x[1].lower().endswith(".pdf")]
    if len(pdfs) > 3:
        out.append({"type": "statement", "text": f"{len(pdfs)} new Awaed confirmations received",
                    "ref": [sha for sha, _ in pdfs]})
        fresh = [x for x in fresh if x not in pdfs]
    for sha, name in fresh:
        out.append({"type": "statement", "text": f"New statement received: {name}", "ref": sha})
    if prev is None:
        out.append({"type": "first", "text": "First report: nothing to compare with."})
        return out
    if prev["as_of"] != new["as_of"]:
        out.append({"type": "as_of", "text": f"Report date moved from {prev['as_of']} to {new['as_of']}",
                    "old": prev["as_of"], "new": new["as_of"]})

    old_h = {h["ident"]: h for h in prev["holdings"]}
    for h in new["holdings"]:
        o = old_h.get(h["ident"])
        label = f"{h['channel'].title()} {h['ident']}"
        if o is None:
            if h["state"] == "closed":
                out.append({"type": "position", "text": f"{label}: {Decimal(h['principal']):,.2f} added and already "
                            f"closed, net profit {Decimal(h['realised_net']):,.2f}", "refs": h["refs"]})
            else:
                out.append({"type": "position", "text": f"{label}: new position of {Decimal(h['principal']):,.2f}, "
                            f"matures {h['maturity']}", "refs": h["refs"]})
        elif o["state"] != h["state"]:
            extra = ""
            if h["state"] == "closed":
                extra = f", paid {h['paid_on']}, net profit {Decimal(h['realised_net']):,.2f}"
            out.append({"type": "status", "text": f"{label}: {o['state']} → {h['state']}{extra}",
                        "old": o["state"], "new": h["state"], "refs": h["refs"]})
    new_ids = {h["ident"] for h in new["holdings"]}
    for ident, o in old_h.items():
        if ident not in new_ids:
            out.append({"type": "position", "text": f"{o['channel'].title()} {ident} no longer in the report",
                        "refs": o["refs"]})

    for key, label, unit in HEADLINE:
        a = prev["figures"].get(key, {}).get("value")
        b = new["figures"].get(key, {}).get("value")
        if _moved(a, b, unit):
            out.append({"type": "figure", "key": key, "text": f"{label}: {_fmt(a, unit)} → {_fmt(b, unit)}",
                        "old": a, "new": b, "basis": new["figures"].get(key, {}).get("basis", [])})

    old_p = {r["check"]: r["status"] for r in prev["tables"].get("policy", [])}
    for r in new["tables"].get("policy", []):
        if r["check"] in old_p and old_p[r["check"]] != r["status"]:
            out.append({"type": "policy", "text": f"{r['check']}: {old_p[r['check']]} → {r['status']}"})
    return out
