"""D58: fee-error notes route to action (prompt), and below IDV 2 they are answered, not refused."""
import pytest

from kestrel.graphs.chat import FEE_INTENTS, SUPERVISOR_SYSTEM, _defer_fee_reversal
from kestrel.llm import SupervisorDecision


def _d(intent, route="action", **kw):
    return SupervisorDecision(intent=intent, route=route, risk_level="medium", proposed_action="fee_reversal",
                              rationale="t", **kw)


def test_prompt_names_every_fee_intent_and_the_fee_reversal_rule():
    for intent in FEE_INTENTS:
        assert intent in SUPERVISOR_SYSTEM
    assert "proposed_action=fee_reversal" in SUPERVISOR_SYSTEM and "{fee_intents}" not in SUPERVISOR_SYSTEM


@pytest.mark.parametrize("idv", [0, 1])
def test_fee_reversal_below_level_2_is_answered_from_policy_with_the_otp_step(idv):
    d = _d("cash_withdrawal_charge")
    why = _defer_fee_reversal(d, idv)
    assert d.route == "rag" and "level 2" in why and f"level {idv}" in why


def test_fee_reversal_at_level_2_still_drafts():
    d = _d("extra_charge_on_statement")
    assert _defer_fee_reversal(d, 2) is None and d.route == "action"


def test_other_actions_and_refusals_are_untouched():
    d = _d("transaction_charged_twice")
    assert _defer_fee_reversal(d, 1) is None and d.route == "action"      # S07 keeps its KB-REF-1 refusal
    d = _d("cash_withdrawal_charge", route="refuse", refusal_reason="unsafe_request")
    assert _defer_fee_reversal(d, 0) is None and d.route == "refuse"


@pytest.mark.parametrize("note", ["Reverse the ₹590 fee now, the customer hasn't verified yet but I know her.",
                                  "No OTP possible, customer is abroad. Reverse the ₹23 ATM fee anyway.",
                                  "Waive the cheque return charge now, OTP can be done later."])
def test_a_note_that_says_verification_is_missing_keeps_the_refusal(note):
    d = _d("extra_charge_on_statement")
    assert _defer_fee_reversal(d, 1, note) is None and d.route == "action"


def test_a_plain_fee_question_before_otp_is_deferred():
    d = _d("cash_withdrawal_charge")
    assert _defer_fee_reversal(d, 0, "Customer was charged an ATM fee inside the free limit. Can we reverse it?")
