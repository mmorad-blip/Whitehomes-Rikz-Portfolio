from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .errors import Rejected

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class MandateRule:
    funded_from: date
    min_principal: Decimal

    def describe(self) -> str:
        return f"funded on or after {self.funded_from:%d %b %Y} with principal of at least {self.min_principal:,.2f} SAR"


@dataclass(frozen=True)
class Contribution:
    date: date
    channel: str
    amount: Decimal
    bank_fee: Decimal


def _load(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Rejected(f"configuration file {path.name} could not be read: {exc}") from None


def load_mandate(path: Path | None = None) -> MandateRule:
    raw = _load(path or CONFIG_DIR / "mandate.toml")["manafa"]
    return MandateRule(funded_from=raw["funded_from"], min_principal=Decimal(raw["min_principal"]))


def load_ledger(path: Path | None = None) -> list[Contribution]:
    raw = _load(path or CONFIG_DIR / "capital_ledger.toml")
    out = []
    for c in raw.get("contribution", []):
        if c["channel"] not in ("awaed", "manafa"):
            raise Rejected(f"capital ledger: unknown channel {c['channel']!r}")
        out.append(Contribution(c["date"], c["channel"], Decimal(c["amount"]), Decimal(c["bank_fee"])))
    return sorted(out, key=lambda c: c.date)
