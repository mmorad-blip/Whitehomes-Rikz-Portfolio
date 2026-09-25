"""Small server-side SVG charts, so pages and PDFs need no scripts or
external assets. Legends are rendered in HTML by the template (readable at
any width); the SVG carries bars, axes and short tick labels only."""

from __future__ import annotations

from decimal import Decimal
from html import escape

# Fixed colour slot per meaning, so the same thing is the same colour everywhere.
SLOTS = {
    "Awaed": 1, "Manafa": 2, "Profit": 3, "Target": 5, "Actual": 1,
    "Performing principal": 1, "Delayed / defaulted, net of provision": 4, "Accrued income": 3, "Wallet cash": 2,
}
HEX = {1: "#1f5f8b", 2: "#3a9d8f", 3: "#e0a100", 4: "#c0504d", 5: "#7f6fb0"}


def colour(name: str, i: int = 0, literal: bool = False) -> str:
    """CSS variable (follows light/dark theme) or, for the PDF renderer, which
    does not resolve variables inside SVG, the literal colour."""
    slot = SLOTS.get(name, i % 5 + 1)
    return HEX[slot] if literal else f"var(--c{slot},{HEX[slot]})"


def _k(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}m"
    if a >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def bars(labels: list[str], series: list[tuple[str, list[Decimal]]], *, height: int = 200, stacked: bool = False,
         title: str = "", literal: bool = False, slot_width: int = 44) -> str:
    """Vertical bar chart; several series side by side, or stacked."""
    n = len(labels)
    width = max(360, slot_width * n + 56)
    top, bottom, left = 12, 26, 44
    vals = [[float(v or 0) for v in s] for _, s in series]
    if stacked:
        vmax = max([sum(max(0.0, s[i]) for s in vals) for i in range(n)] + [0.0])
        vmin = min([sum(min(0.0, s[i]) for s in vals) for i in range(n)] + [0.0])
    else:
        vmax = max([v for s in vals for v in s] + [0.0])
        vmin = min([v for s in vals for v in s] + [0.0])
    span = (vmax - vmin) or 1.0
    ph = height - top - bottom
    y = lambda v: top + (vmax - v) / span * ph  # noqa: E731
    slot = (width - left - 4) / max(n, 1)
    bw = slot * 0.72 / (1 if stacked else len(series))
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" class="chart">']
    ticks = sorted({vmin, vmax, 0.0} | ({(vmin + vmax) / 2} if vmin == 0 else set()))
    for t in ticks:
        out.append(f'<line x1="{left}" x2="{width - 2}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>')
        out.append(f'<text x="{left - 5}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{_k(t)}</text>')
    for i, lab in enumerate(labels):
        x0 = left + i * slot + slot * 0.14
        base_pos = base_neg = 0.0
        for j, (name, _) in enumerate(series):
            v = vals[j][i]
            if stacked:
                lo, hi = (base_pos, base_pos + v) if v >= 0 else (base_neg + v, base_neg)
                if v >= 0:
                    base_pos += v
                else:
                    base_neg += v
                x = x0
            else:
                lo, hi = (0.0, v) if v >= 0 else (v, 0.0)
                x = x0 + j * bw
            if hi - lo > 0:
                out.append(f'<rect x="{x:.1f}" y="{y(hi):.1f}" width="{bw:.1f}" height="{max(y(lo) - y(hi), 0.5):.1f}" '
                           f'fill="{colour(name, j, literal)}"><title>{escape(name)} · {escape(lab)}: {v:,.2f}</title></rect>')
        out.append(f'<text x="{left + i * slot + slot / 2:.1f}" y="{height - 8}" text-anchor="middle" '
                   f'class="tick">{escape(lab)}</text>')
    out.append("</svg>")
    return "".join(out)


def share_bar(parts: list[tuple[str, Decimal]], *, title: str = "", literal: bool = False) -> str:
    """One horizontal bar split into parts (e.g. NAV composition)."""
    width, height = 600, 36
    total = sum(float(v) for _, v in parts if v and v > 0) or 1.0
    x = 0.0
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" class="chart share">']
    for j, (name, v) in enumerate(parts):
        if not v or v <= 0:
            continue
        w = float(v) / total * width
        out.append(f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{height}" fill="{colour(name, j, literal)}">'
                   f'<title>{escape(name)}: {float(v):,.2f} ({float(v) / total:.1%})</title></rect>')
        x += w
    out.append("</svg>")
    return "".join(out)


def legend(names: list[str], literal: bool = False) -> list[tuple[str, str]]:
    return [(n, colour(n, i, literal)) for i, n in enumerate(names)]
