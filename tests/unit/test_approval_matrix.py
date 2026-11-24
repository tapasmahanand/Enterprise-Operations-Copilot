import pytest

from kestrel.rules.approval import approval_requirement
from kestrel.rules.loader import load_rules

RULES = load_rules()


@pytest.mark.parametrize(
    "amount,role",
    [(18450, "team_lead"), (25000, "team_lead"), (25001, "ops_manager"),
     (200000, "ops_manager"), (200001, "regional_ops_head")],
)
def test_credit_tier_by_amount(amount, role):
    req = approval_requirement("credit", amount, RULES)
    assert req.needs_approval is True
    assert req.approver_role == role


def test_every_refund_needs_approval_even_tiny_ones():
    assert approval_requirement("credit", 1, RULES).needs_approval is True


def test_repeat_dispute_goes_one_tier_up():
    req = approval_requirement("chargeback", 18450, RULES, repeat_dispute=True)
    assert req.approver_role == "ops_manager"
    assert "repeat_dispute" in req.reasons


def test_repeat_dispute_at_top_tier_stays_top():
    req = approval_requirement("credit", 500000, RULES, repeat_dispute=True)
    assert req.approver_role == "regional_ops_head"


def test_card_block_needs_no_approval():
    req = approval_requirement("card_block", None, RULES)
    assert req.needs_approval is False
    assert req.approver_role is None


def test_unverified_identity_is_refused():
    req = approval_requirement("credit", 18450, RULES, identity_verified=False)
    assert req.refused is True
    assert req.refusal_rule == "KB-REF-1"


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        approval_requirement("wire_out", 100, RULES)


def test_tiered_action_needs_amount():
    with pytest.raises(ValueError):
        approval_requirement("fee_reversal", None, RULES)
