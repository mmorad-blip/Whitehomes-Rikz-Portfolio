"""Parsed records. Every record carries where it came from (file hash and row
or page) so any figure built on it can be traced back to a statement line."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from .vocab import Status, Txn


@dataclass(frozen=True)
class Source:
    """An immutable uploaded file, identified by its content hash."""

    sha256: str
    name: str
    channel: str  # "awaed" | "manafa"
    kind: str  # "murabaha_confirmation" | "portfolio_export" | "account_statement"


@dataclass(frozen=True)
class AwaedDeposit:
    source: Source
    order_id: str
    ordered_at: datetime
    product: str
    tenor_days: int
    principal: Decimal
    total_return: Decimal  # as printed, full precision
    fees: Decimal
    vat: Decimal
    total_amount: Decimal

    @property
    def order_date(self) -> date:
        return self.ordered_at.date()

    @property
    def maturity(self) -> date:
        return self.order_date + timedelta(days=self.tenor_days)


@dataclass(frozen=True)
class ManafaPosition:
    """One row of the portfolio export. Split investments share an OID but are
    separate rows (and separate positions).

    Net profit: on the closed sheet a split OID repeats the OID's total net
    profit on each of its rows; on the open sheet each row carries its own.
    """

    source: Source
    sheet: str
    row: int
    number: int
    oid: str
    tenor_months: int
    rate: Decimal  # contract return on the position, e.g. 0.0360
    rating: str
    principal: Decimal
    net_profit: Decimal  # for split OIDs the export repeats the per-OID total
    total: Decimal  # expected (open) or received (closed)
    entry: date
    maturity: date
    status: Status
    # 1 for the first row with this OID + entry date + principal in the export,
    # 2 for the second, ... The platform does list two genuinely separate
    # investments with identical OID, date and amount (each has its own
    # investment row on the account statement).
    occurrence: int = 1

    @property
    def key(self) -> tuple[str, date, Decimal, int]:
        """Dedup key across exports: OID + entry date + principal + occurrence."""
        return (self.oid, self.entry, self.principal, self.occurrence)

    @property
    def contract_days(self) -> int:
        return (self.maturity - self.entry).days


@dataclass(frozen=True)
class PortfolioExport:
    source: Source
    open: tuple[ManafaPosition, ...]
    closed: tuple[ManafaPosition, ...]

    @property
    def positions(self) -> tuple[ManafaPosition, ...]:
        return self.open + self.closed


@dataclass(frozen=True)
class StatementRow:
    source: Source
    row: int
    date: date
    ref: str
    type_ar: str
    txn: Txn
    amount: Decimal
    balance: Decimal


@dataclass(frozen=True)
class AccountStatement:
    """Manafa كشف الحساب. The customer block is never read."""

    source: Source
    period_start: date
    period_end: date
    issued_on: date
    opening: Decimal
    total_deposits: Decimal
    total_withdrawals: Decimal
    total_pending: Decimal
    closing: Decimal
    rows: tuple[StatementRow, ...] = field(repr=False)
