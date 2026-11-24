"""Deterministic liability and turnaround-time calculator.

The LLM never does date or rupee arithmetic: it calls these functions and cites the
rule ids they return. Every figure comes from rules/kestrel_rules.yaml, which is
traceable to docs/rbi-sources.md.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from kestrel.rules.loader import Calendar, Rules

Fault = Literal["bank", "third_party", "customer"]
Product = Literal["savings", "bsbd", "ppi", "current_small", "current_large", "credit_card"]


# --- calendar helpers --------------------------------------------------------

def working_days_after(start: date, end: date, cal: Calendar) -> int:
    """Working days in (start, end]: the start date itself is excluded (R17-WD)."""
    if end < start:
        raise ValueError("end before start")
    return sum(1 for i in range(1, (end - start).days + 1) if cal.is_working_day(start + timedelta(days=i)))


def add_working_days(start: date, n: int, cal: Calendar) -> date:
    d, left = start, n
    while left > 0:
        d += timedelta(days=1)
        if cal.is_working_day(d):
            left -= 1
    return d


# --- liability -----------------------------------------------------------------

class LiabilityInput(BaseModel):
    amount_inr: int = Field(gt=0)
    txn_date: date
    reported_on: date
    fault: Fault
    product: Product
    alert_received_on: date | None = None      # R17 clock; defaults to txn_date
    credit_limit_inr: int | None = None
    cross_border: bool = False
    reported_to_ncrp_on: date | None = None    # R26-76T
    prior_compensation_claimed: bool = False   # R26-76T is once per lifetime

    @model_validator(mode="after")
    def _check_dates(self) -> "LiabilityInput":
        clock = self.alert_received_on or self.txn_date
        if self.reported_on < clock or self.reported_on < self.txn_date:
            raise ValueError("reported_on is before the transaction or the bank's alert")
        return self


class LiabilityResult(BaseModel):
    regime: Literal["R17", "R26"]
    customer_liability_inr: int
    bank_liability_inr: int
    compensation_inr: int = 0
    needs_review: bool = False
    shadow_credit_due: date | None = None
    resolution_due: date
    rule_ids: list[str]
    explanation: str


def _r17_cap(inp: LiabilityInput, caps: dict[str, int]) -> int:
    if inp.product == "bsbd":
        return caps["bsbd"]
    if inp.product == "current_large":
        return caps["tier_25k"]
    if inp.product == "credit_card":
        if inp.credit_limit_inr is None:
            raise ValueError("credit_limit_inr is required for credit cards")
        return caps["tier_10k"] if inp.credit_limit_inr <= 500_000 else caps["tier_25k"]
    return caps["tier_10k"]  # savings, ppi, current_small


def _assess_r17(inp: LiabilityInput, reg: dict, rules: Rules) -> LiabilityResult:
    cal = rules.calendar
    ids: list[str] = []
    review = False
    if inp.fault == "bank":
        cust, ids, why = 0, ["R17-6i"], "Bank negligence or deficiency: zero customer liability."
    elif inp.fault == "customer":
        cust, ids, why = inp.amount_inr, ["R17-7i"], "Customer negligence: customer bears the loss until it was reported."
    else:
        delay = working_days_after(inp.alert_received_on or inp.txn_date, inp.reported_on, cal)
        ids.append("R17-WD")
        if delay <= reg["zero_liability_window_working_days"]:
            cust = 0
            ids.append("R17-6ii")
            why = f"Third-party breach reported {delay} working day(s) after the bank's alert: zero liability."
        elif delay <= reg["limited_liability_window_working_days"]:
            cap = _r17_cap(inp, reg["caps_inr"])
            cust = min(inp.amount_inr, cap)
            ids += ["R17-7ii", "R17-T1"]
            why = f"Reported after {delay} working days: liability is the lower of the amount and the ₹{cap:,} cap."
        else:
            cust, review = inp.amount_inr, True
            ids += ["R17-8", "KB-BP-1"]
            why = f"Reported after {delay} working days: Kestrel Board policy applies; case review required."
    bank = inp.amount_inr - cust
    shadow = None
    if bank > 0 and not review:
        shadow = add_working_days(inp.reported_on, reg["shadow_credit_working_days"], cal)
        ids.append("R17-9")
    ids.append("R17-10")
    return LiabilityResult(
        regime="R17", customer_liability_inr=cust, bank_liability_inr=bank, needs_review=review,
        shadow_credit_due=shadow,
        resolution_due=inp.reported_on + timedelta(days=reg["resolution_calendar_days"]),
        rule_ids=ids, explanation=why,
    )


def _r26_compensation(inp: LiabilityInput, cust: int, comp: dict) -> int:
    ncrp = inp.reported_to_ncrp_on
    window = timedelta(days=comp["report_within_calendar_days"])
    eligible = (
        inp.fault == "customer"
        and not inp.prior_compensation_claimed
        and inp.amount_inr <= comp["max_gross_loss_inr"]
        and inp.reported_on - inp.txn_date <= window
        and ncrp is not None
        and ncrp - inp.txn_date <= window
    )
    if not eligible:
        return 0
    return min(int(round(cust * comp["share_of_net_loss"])), comp["cap_inr"])


def _assess_r26(inp: LiabilityInput, reg: dict, rules: Rules) -> LiabilityResult:
    ids: list[str] = ["R26-76K"]
    review = False
    if inp.fault == "bank":
        cust, why = 0, "Bank negligence: zero customer liability irrespective of reporting."
        ids.append("R26-76L")
    elif inp.fault == "customer":
        cust, why = inp.amount_inr, "Customer negligence: customer liable for the loss until reported."
        ids.append("R26-76N")
    else:
        delay = (inp.reported_on - inp.txn_date).days
        if delay <= reg["zero_liability_window_calendar_days"]:
            cust, why = 0, f"Third-party breach reported {delay} calendar day(s) after occurrence: zero liability."
            ids.append("R26-76M")
        else:
            cust, review = inp.amount_inr, True
            why = f"Reported {delay} calendar days after occurrence: Kestrel Board policy applies; case review required."
            ids += ["R26-76M", "KB-BP-1"]

    compensation = _r26_compensation(inp, cust, reg["compensation"])
    if compensation:
        cust -= compensation
        ids.append("R26-76T")
        why += f" Eligible for one-time RBI compensation of ₹{compensation:,}."

    bank = inp.amount_inr - cust
    shadow = None
    if bank > 0 and not review and inp.fault != "customer":
        if inp.product == "credit_card":
            shadow = inp.reported_on + timedelta(days=reg["credit_card_shadow_reversal_calendar_days"])
            ids.append("R26-76R")
        else:
            shadow = add_working_days(inp.reported_on, rules.kestrel_policy["internal_shadow_credit_working_days"], rules.calendar)
            ids.append("KB-INT-SC")
    res = reg["resolution_calendar_days"]["cross_border" if inp.cross_border else "domestic"]
    ids.append("R26-76Q")
    return LiabilityResult(
        regime="R26", customer_liability_inr=cust, bank_liability_inr=bank, compensation_inr=compensation,
        needs_review=review, shadow_credit_due=shadow,
        resolution_due=inp.reported_on + timedelta(days=res), rule_ids=ids, explanation=why,
    )


def assess_liability(inp: LiabilityInput, rules: Rules) -> LiabilityResult:
    reg = rules.regime_for(inp.txn_date)
    return _assess_r17(inp, reg, rules) if reg["id"] == "R17" else _assess_r26(inp, reg, rules)


# --- failed-transaction TAT (R19) ---------------------------------------------

class TatResult(BaseModel):
    rule_id: str
    reversal_due: date
    days_late: int
    compensation_inr: int


def failed_txn_compensation(
    channel: str, txn_date: date, resolved_on: date | None, rules: Rules, as_of: date | None = None
) -> TatResult:
    tat = rules.failed_txn_tat
    row = tat["channels"].get(channel)
    if row is None:
        raise ValueError(f"channel {channel!r} is unconfirmed or unknown; see docs/rbi-sources.md")
    due = txn_date + timedelta(days=row["reversal_days"])  # T is a calendar date
    end = resolved_on or as_of
    if end is None:
        raise ValueError("give resolved_on or as_of")
    late = max(0, (end - due).days)
    return TatResult(rule_id=row["rule"], reversal_due=due, days_late=late,
                     compensation_inr=late * tat["compensation_inr_per_day"])
