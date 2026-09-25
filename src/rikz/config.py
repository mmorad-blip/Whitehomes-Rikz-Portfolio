from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .errors import Rejected

ROOT = Path(__file__).resolve().parents[2]
# The repository's config/ in development; RIKZ_CONFIG_DIR when installed.
CONFIG_DIR = Path(os.environ.get("RIKZ_CONFIG_DIR", str(ROOT / "config")))


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
    owner: str = ""

    @property
    def ref(self) -> str:
        return f"capital ledger {self.date} {self.channel} {self.amount:,.2f}"


@dataclass(frozen=True)
class Settings:
    days_per_year: int
    provision_default: Decimal
    provision_delay: Decimal
    accrue_open_profit: bool
    upcoming_days: int
    forecast_loss_per_cycle: Decimal
    benchmark_saibor: Decimal
    benchmark_is_placeholder: bool
    awaed_idle_days: int
    budget: dict[str, Decimal]
    target_low: Decimal
    target_high: Decimal
    allocation: dict[str, Decimal]
    expected_return: dict[str, tuple[Decimal, Decimal]]
    limits: dict[str, Decimal]


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
        if c.get("owner") not in ("Whitehomes", "Rikz"):
            raise Rejected(f"capital ledger: contribution on {c['date']} needs owner Whitehomes or Rikz")
        out.append(Contribution(c["date"], c["channel"], Decimal(c["amount"]), Decimal(c["bank_fee"]), c["owner"]))
    return sorted(out, key=lambda c: c.date)


def load_settings(path: Path | None = None) -> Settings:
    raw = _load(path or CONFIG_DIR / "settings.toml")
    c, p = raw["calculation"], raw["policy"]
    settings = Settings(
        days_per_year=int(c["days_per_year"]),
        provision_default=Decimal(c["provision_default"]),
        provision_delay=Decimal(c["provision_delay"]),
        accrue_open_profit=bool(c["accrue_open_profit"]),
        upcoming_days=int(c["upcoming_days"]),
        forecast_loss_per_cycle=Decimal(c["forecast_loss_per_cycle"]),
        benchmark_saibor=Decimal(c["benchmark_saibor"]),
        benchmark_is_placeholder=bool(c.get("benchmark_is_placeholder", False)),
        awaed_idle_days=int(c["awaed_idle_days"]),
        budget={"Whitehomes": Decimal(p["budget_whitehomes"]), "Rikz": Decimal(p["budget_rikz"])},
        target_low=Decimal(p["target_return_low"]),
        target_high=Decimal(p["target_return_high"]),
        allocation={k: Decimal(v) for k, v in p["allocation"].items()},
        expected_return={k: (Decimal(v[0]), Decimal(v[1])) for k, v in p["expected_return"].items()},
        limits={k: Decimal(v) for k, v in p["limits"].items()},
    )
    if sum(settings.allocation.values()) != 1:
        raise Rejected("settings: policy allocation targets must add up to 100%")
    return settings
