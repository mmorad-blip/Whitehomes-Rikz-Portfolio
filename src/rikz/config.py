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
    bank_ref: str = ""
    evidence: str = ""

    @property
    def ref(self) -> str:
        return f"capital ledger {self.date} {self.channel} {self.amount:,.2f}"

    @property
    def evidenced(self) -> bool:
        return bool(self.bank_ref and self.evidence)


@dataclass(frozen=True)
class Recovery:
    """Collection status of one defaulted position, kept by hand in settings."""

    expected: Decimal = Decimal("0")
    status: str = "in collection"  # in collection | legal | settled | written off
    last_action: date | None = None
    last_action_note: str = ""


RECOVERY_STATUSES = ("in collection", "legal", "settled", "written off")


@dataclass(frozen=True)
class Settings:
    days_per_year: int
    provision_default: Decimal
    provision_delay: Decimal
    accrue_open_profit: bool
    upcoming_days: int
    # None = use the observed loss rate (defaulted principal net of recoveries ÷ principal funded).
    forecast_loss_per_cycle: Decimal | None
    benchmark_saibor: Decimal
    benchmark_is_placeholder: bool
    awaed_idle_days: int
    budget: dict[str, Decimal]
    target_low: Decimal
    target_high: Decimal
    allocation: dict[str, Decimal]
    expected_return: dict[str, tuple[Decimal, Decimal]]
    limits: dict[str, Decimal]
    benchmark_as_of: date | None = None
    benchmark_source: str = ""
    writeoff_days: int = 180
    idle_grace_days: int = 2
    awaed_rounding_tolerance: Decimal = Decimal("1.00")
    recovery: dict[str, Recovery] | None = None

    def recovery_for(self, ident: str) -> Recovery:
        return (self.recovery or {}).get(ident, Recovery())

    def benchmark_warning(self, as_of: date) -> str:
        if self.benchmark_is_placeholder:
            return f"The {self.benchmark_saibor:.2%} 3M SAIBOR benchmark is a placeholder; enter the published rate, its date and source"
        if self.benchmark_as_of is None:
            return "The 3M SAIBOR benchmark has no as-of date"
        age = (as_of - self.benchmark_as_of).days
        if age > 30:
            return f"The 3M SAIBOR benchmark is {age} days old (as of {self.benchmark_as_of:%d %b %Y})"
        return ""


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
        out.append(Contribution(c["date"], c["channel"], Decimal(c["amount"]), Decimal(c["bank_fee"]), c["owner"],
                                c.get("bank_ref", ""), c.get("evidence", "")))
    return sorted(out, key=lambda c: c.date)


@dataclass(frozen=True)
class SettlementRule:
    rule: str
    approved_by: str
    approved_on: date | None

    def describe(self) -> str:
        who = f"approved by {self.approved_by}" if self.approved_by else "NOT YET APPROVED"
        when = f" on {self.approved_on:%d %b %Y}" if self.approved_on else ""
        return f"{self.rule} ({who}{when})"


def load_settlement_rule(path: Path | None = None) -> SettlementRule:
    raw = _load(path or CONFIG_DIR / "mandate.toml").get("settlement", {})
    return SettlementRule(raw.get("rule", "No rule recorded"), raw.get("approved_by", ""), raw.get("approved_on"))


@dataclass(frozen=True)
class ConfigChange:
    date: date
    who: str
    file: str
    setting: str
    old: str
    new: str
    reason: str = ""


def load_change_log(path: Path | None = None) -> list[ConfigChange]:
    p = path or CONFIG_DIR / "change_log.toml"
    if not p.exists():
        return []
    raw = _load(p)
    return sorted((ConfigChange(e["date"], e["who"], e["file"], e["setting"], str(e.get("old", "")), str(e.get("new", "")),
                                e.get("reason", "")) for e in raw.get("change", [])), key=lambda e: e.date, reverse=True)


def load_settings(path: Path | None = None) -> Settings:
    raw = _load(path or CONFIG_DIR / "settings.toml")
    c, p = raw["calculation"], raw["policy"]
    settings = Settings(
        days_per_year=int(c["days_per_year"]),
        provision_default=Decimal(c["provision_default"]),
        provision_delay=Decimal(c["provision_delay"]),
        accrue_open_profit=bool(c["accrue_open_profit"]),
        upcoming_days=int(c["upcoming_days"]),
        forecast_loss_per_cycle=(None if str(c["forecast_loss_per_cycle"]).lower() == "observed"
                                 else Decimal(c["forecast_loss_per_cycle"])),
        benchmark_as_of=c.get("benchmark_as_of"),
        benchmark_source=c.get("benchmark_source", ""),
        writeoff_days=int(c.get("writeoff_days", 180)),
        idle_grace_days=int(c.get("idle_grace_days", 2)),
        awaed_rounding_tolerance=Decimal(c.get("awaed_rounding_tolerance", "1.00")),
        recovery={k: Recovery(Decimal(v.get("expected", "0")), v.get("status", "in collection"),
                              v.get("last_action"), v.get("last_action_note", ""))
                  for k, v in raw.get("recovery", {}).items()},
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
    for k, rec in (settings.recovery or {}).items():
        if rec.status not in RECOVERY_STATUSES:
            raise Rejected(f"settings: recovery status for {k} must be one of {', '.join(RECOVERY_STATUSES)}")
        if not ZERO_ <= rec.expected <= 1:
            raise Rejected(f"settings: expected recovery for {k} must be between 0 and 1")
    return settings


ZERO_ = Decimal("0")
