from datetime import date

import pytest

from kestrel.rules.calculator import (
    LiabilityInput,
    add_working_days,
    assess_liability,
    failed_txn_compensation,
    working_days_after,
)
from kestrel.rules.loader import load_rules

RULES = load_rules()
CAL = RULES.calendar


# --- calendar -------------------------------------------------------------

def test_working_days_skip_sunday_and_second_saturday():
    # Fri 11 Dec 2026 → Sat 12 is the 2nd Saturday (closed), Sun 13 closed, Mon 14 open.
    assert working_days_after(date(2026, 12, 11), date(2026, 12, 14), CAL) == 1


def test_first_saturday_is_a_working_day():
    # Sat 5 Dec 2026 is the 1st Saturday of the month → open.
    assert working_days_after(date(2026, 12, 4), date(2026, 12, 5), CAL) == 1


def test_same_day_report_is_zero_working_days():
    assert working_days_after(date(2026, 12, 1), date(2026, 12, 1), CAL) == 0


def test_add_working_days_skips_christmas_and_weekend():
    # From Wed 23 Dec 2026: Thu 24 (1), Fri 25 holiday, Sat 26 is the 4th Saturday,
    # Sun 27 closed, Mon 28 (2).
    assert add_working_days(date(2026, 12, 23), 2, CAL) == date(2026, 12, 28)


# --- R17 regime (transactions before 1 Jan 2027) --------------------------

def _r17(**kw):
    base = dict(
        amount_inr=18450,
        txn_date=date(2026, 12, 1),
        alert_received_on=date(2026, 12, 1),
        reported_on=date(2026, 12, 2),
        fault="third_party",
        product="savings",
    )
    base.update(kw)
    return LiabilityInput(**base)


def test_hero_case_zero_liability_within_three_working_days():
    r = assess_liability(_r17(), RULES)
    assert r.regime == "R17"
    assert r.customer_liability_inr == 0
    assert r.bank_liability_inr == 18450
    assert "R17-6ii" in r.rule_ids
    # Reported Wed 2 Dec 2026; 10 working days later. Sat 5 Dec (1st Sat) is open,
    # Sat 12 (2nd) closed: 3,4,5,7,8,9,10,11,14,15 → Tue 15 Dec.
    assert r.shadow_credit_due == date(2026, 12, 15)
    assert "R17-9" in r.rule_ids
    assert r.resolution_due == date(2027, 3, 2)  # 90 calendar days after 2 Dec 2026


def test_third_working_day_is_still_zero_and_fourth_is_limited():
    zero = assess_liability(_r17(reported_on=date(2026, 12, 4)), RULES)  # Thu,Fri → wd 3 = Fri 4
    assert zero.customer_liability_inr == 0
    limited = assess_liability(_r17(reported_on=date(2026, 12, 5)), RULES)  # Sat 5 = wd 4
    assert limited.customer_liability_inr == 10000
    assert {"R17-7ii", "R17-T1"} <= set(limited.rule_ids)


@pytest.mark.parametrize(
    "product,limit,amount,expected",
    [
        ("bsbd", None, 18450, 5000),
        ("savings", None, 3000, 3000),
        ("current_large", None, 40000, 25000),
        ("credit_card", 300000, 18450, 10000),
        ("credit_card", 600000, 40000, 25000),
    ],
)
def test_limited_liability_caps(product, limit, amount, expected):
    r = assess_liability(
        _r17(product=product, credit_limit_inr=limit, amount_inr=amount, reported_on=date(2026, 12, 7)),
        RULES,
    )
    assert r.customer_liability_inr == expected
    assert r.bank_liability_inr == amount - expected


def test_beyond_seven_working_days_falls_to_board_policy_and_needs_review():
    r = assess_liability(_r17(reported_on=date(2026, 12, 21)), RULES)
    assert r.customer_liability_inr == 18450
    assert r.needs_review is True
    assert {"R17-8", "KB-BP-1"} <= set(r.rule_ids)
    assert r.shadow_credit_due is None


def test_bank_fault_is_zero_liability_even_when_reported_late():
    r = assess_liability(_r17(fault="bank", reported_on=date(2026, 12, 30)), RULES)
    assert r.customer_liability_inr == 0
    assert "R17-6i" in r.rule_ids


def test_customer_negligence_bears_loss_until_reported():
    r = assess_liability(_r17(fault="customer"), RULES)
    assert r.customer_liability_inr == 18450
    assert r.shadow_credit_due is None
    assert "R17-7i" in r.rule_ids


def test_report_before_alert_is_rejected():
    with pytest.raises(ValueError):
        assess_liability(_r17(reported_on=date(2026, 11, 30)), RULES)


# --- R26 regime (transactions on/after 1 Jan 2027) -------------------------

def _r26(**kw):
    base = dict(
        amount_inr=18450,
        txn_date=date(2027, 1, 4),
        reported_on=date(2027, 1, 9),
        fault="third_party",
        product="credit_card",
        credit_limit_inr=300000,
    )
    base.update(kw)
    return LiabilityInput(**base)


def test_regime_switches_on_first_of_january_2027():
    assert assess_liability(_r26(txn_date=date(2026, 12, 31), reported_on=date(2027, 1, 1)), RULES).regime == "R17"
    assert assess_liability(_r26(txn_date=date(2027, 1, 1), reported_on=date(2027, 1, 2)), RULES).regime == "R26"


def test_r26_zero_liability_within_five_calendar_days_of_occurrence():
    r = assess_liability(_r26(), RULES)  # 4 Jan → 9 Jan = 5 calendar days
    assert r.customer_liability_inr == 0
    assert "R26-76M" in r.rule_ids
    assert r.shadow_credit_due == date(2027, 1, 14)  # credit card: 5 calendar days
    assert "R26-76R" in r.rule_ids
    assert r.resolution_due == date(2027, 2, 23)  # 45 calendar days


def test_r26_sixth_day_falls_to_board_policy():
    r = assess_liability(_r26(reported_on=date(2027, 1, 10)), RULES)
    assert r.customer_liability_inr == 18450
    assert r.needs_review is True
    assert "KB-BP-1" in r.rule_ids


def test_r26_non_card_shadow_credit_uses_internal_standard():
    r = assess_liability(_r26(product="savings", credit_limit_inr=None), RULES)
    assert "KB-INT-SC" in r.rule_ids
    assert r.shadow_credit_due == add_working_days(date(2027, 1, 9), 10, CAL)


def test_r26_cross_border_resolution_is_sixty_days():
    r = assess_liability(_r26(cross_border=True), RULES)
    assert r.resolution_due == date(2027, 3, 10)


@pytest.mark.parametrize(
    "amount,ncrp,prior,expected",
    [
        (20000, date(2027, 1, 6), False, 17000),   # 85% of 20,000
        (40000, date(2027, 1, 6), False, 25000),   # capped
        (60000, date(2027, 1, 6), False, 0),       # gross loss above ₹50,000
        (20000, None, False, 0),                   # not reported to NCRP/1930
        (20000, date(2027, 1, 10), False, 0),      # NCRP report after 5 days
        (20000, date(2027, 1, 6), True, 0),        # once per lifetime
    ],
)
def test_r26_one_time_compensation(amount, ncrp, prior, expected):
    r = assess_liability(
        _r26(fault="customer", amount_inr=amount, reported_on=date(2027, 1, 6),
             reported_to_ncrp_on=ncrp, prior_compensation_claimed=prior),
        RULES,
    )
    assert r.customer_liability_inr == amount - expected
    assert r.compensation_inr == expected
    if expected:
        assert "R26-76T" in r.rule_ids


# --- R19 failed-transaction TAT ---------------------------------------------

def test_upi_transfer_late_reversal_pays_100_per_day():
    r = failed_txn_compensation("upi_transfer", date(2026, 12, 1), date(2026, 12, 5), RULES)
    assert r.reversal_due == date(2026, 12, 2)
    assert r.days_late == 3
    assert r.compensation_inr == 300
    assert r.rule_id == "R19-4a"


def test_atm_reversal_within_tat_pays_nothing():
    r = failed_txn_compensation("atm", date(2026, 12, 1), date(2026, 12, 6), RULES)
    assert r.compensation_inr == 0


def test_unresolved_transaction_accrues_until_as_of_date():
    r = failed_txn_compensation("imps", date(2026, 12, 1), None, RULES, as_of=date(2026, 12, 4))
    assert r.days_late == 2
    assert r.compensation_inr == 200


def test_card_not_present_is_refused_until_figures_are_confirmed():
    with pytest.raises(ValueError, match="unconfirmed"):
        failed_txn_compensation("card_not_present", date(2026, 12, 1), date(2026, 12, 9), RULES)
