"""D48: the LLM may not refuse an in-scope request; only an unsafe request keeps its refusal."""
import pytest

from kestrel.graphs.chat import _refusal_policy
from kestrel.llm import SupervisorDecision


def d(intent, reason=None, route="refuse"):
    return SupervisorDecision(intent=intent, route=route, risk_level="low", proposed_action="none",
                              refusal_reason=reason, rationale="x")


@pytest.mark.parametrize("intent,expected", [("transaction_charged_twice", "action"), ("request_refund", "tools"),
                                             ("cash_withdrawal_charge", "rag"), ("card_payment_not_recognised", "action")])
def test_topical_refusal_of_an_in_scope_intent_is_overridden_to_its_default_route(intent, expected):
    x = d(intent, "not_a_desk_topic")
    assert _refusal_policy(x, idv=0) and x.route == expected


def test_refusal_without_a_reason_is_overridden_too():
    x = d("transaction_charged_twice")
    assert _refusal_policy(x, idv=2) and x.route == "action"


def test_unsafe_refusal_stands_for_a_non_fraud_intent_at_any_level():
    for idv in (0, 1, 2, 3):
        x = d("extra_charge_on_statement", "unsafe_request")
        assert _refusal_policy(x, idv) is None and x.route == "refuse"


def test_unsafe_fraud_report_stands_at_level_0_but_blocks_the_card_from_level_1():
    x = d("compromised_card", "unsafe_request")
    assert _refusal_policy(x, 0) is None and x.route == "refuse"
    x = d("compromised_card", "unsafe_request")
    assert _refusal_policy(x, 1) and x.route == "action"


def test_out_of_scope_and_non_refusals_are_untouched():
    x = d("out_of_scope", "not_a_desk_topic")
    assert _refusal_policy(x, 2) is None and x.route == "refuse"
    x = d("request_refund", route="tools")
    assert _refusal_policy(x, 2) is None and x.route == "tools"
