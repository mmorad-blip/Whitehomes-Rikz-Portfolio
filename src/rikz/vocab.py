"""Words the platforms use, mapped to our terms. An unknown word rejects the
file: a new status or transaction type must be added here deliberately."""

from __future__ import annotations

import re
from enum import Enum

from .errors import Rejected


class Status(str, Enum):
    ACTIVE = "active"
    DELAYED = "delayed"
    DEFAULTED = "defaulted"
    REPAID = "repaid"
    REPAID_EARLY = "repaid_early"

    @property
    def closed(self) -> bool:
        return self in (Status.REPAID, Status.REPAID_EARLY)

    @property
    def troubled(self) -> bool:
        return self in (Status.DELAYED, Status.DEFAULTED)


MANAFA_STATUS = {
    "نشط": Status.ACTIVE,
    "متأخرة": Status.DELAYED,
    "متعثرة": Status.DEFAULTED,
    "تم السداد": Status.REPAID,
    "سداد مبكر": Status.REPAID_EARLY,
}


class Txn(str, Enum):
    DEPOSIT = "deposit"  # money in from outside (bank transfer, Apple Pay)
    REWARD = "reward"  # platform reward credited to the wallet
    REDEPOSIT = "redeposit"  # a withdrawal returned to the wallet
    WITHDRAWAL = "withdrawal"  # money out to the bank
    INVESTMENT = "investment"  # principal placed in an opportunity
    PRINCIPAL = "principal"  # principal repaid
    PROFIT = "profit"  # gross profit paid
    FEE = "fee"  # agency fee charged (negative)
    FEE_REBATE = "fee_rebate"  # agency fee refunded (positive)
    VAT = "vat"  # VAT on the agency fee


# (reference prefix, Arabic type) -> meaning. The pair must match: a known
# word under an unexpected reference prefix is as suspicious as a new word.
MANAFA_TXN = {
    ("MDWD", "حساب بنكي"): Txn.DEPOSIT,
    ("MDWD", "ابل باي"): Txn.DEPOSIT,
    ("MDLP", "مكافأة"): Txn.REWARD,
    ("MRWD", "إعادة إيداع مبلغ"): Txn.REDEPOSIT,
    ("MWWD", "سحب نقدي"): Txn.WITHDRAWAL,
    ("MIWD", "استثمار"): Txn.INVESTMENT,
    ("MIPA", "مبلغ الاستثمار"): Txn.PRINCIPAL,
    ("MIPP", "عائد الاستثمار"): Txn.PROFIT,
    ("MIPF", "أجرة الوكالة"): Txn.FEE,
    ("MIPF", "استرجاع مبلغ الخصم"): Txn.FEE_REBATE,
    ("MIPV", "ضريبة القيمة المضافة"): Txn.VAT,
}

REPAYMENT_PARTS = {Txn.PRINCIPAL, Txn.PROFIT, Txn.FEE, Txn.FEE_REBATE, Txn.VAT}


def manafa_status(raw: object) -> Status:
    word = raw.strip() if isinstance(raw, str) else raw
    if word not in MANAFA_STATUS:
        raise Rejected(f"unknown position status {raw!r}")
    return MANAFA_STATUS[word]


def manafa_txn(ref: object, raw_type: object) -> Txn:
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Z]{4}\d{16}", ref):
        raise Rejected(f"reference {ref!r} is not in the expected shape (4 letters + 16 digits)")
    word = raw_type.strip() if isinstance(raw_type, str) else raw_type
    key = (ref[:4], word)
    if key not in MANAFA_TXN:
        raise Rejected(f"unknown transaction type {raw_type!r} for reference {ref}")
    return MANAFA_TXN[key]


def manafa_tenor_months(raw: object) -> int:
    """'3 اشهر ' -> 3. The contractual label only; accrual uses actual days."""
    m = re.fullmatch(r"\s*(\d+)\s+(?:اشهر|أشهر|شهر)\s*", raw) if isinstance(raw, str) else None
    if not m:
        raise Rejected(f"unknown tenor {raw!r}")
    return int(m.group(1))


def manafa_rating(raw: object) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[A-D]{1,3}[+-]?", raw.strip()):
        raise Rejected(f"unknown opportunity rating {raw!r}")
    return raw.strip()


# Awaed product line -> contract days. "1 Month" is taken as 30 days (see
# assumptions in the report); a new product line rejects the file.
AWAED_TENOR_DAYS = {
    "Week Murabaha": 7,
    "1 Week Murabaha": 7,
    "1 Month Murabaha": 30,
}
