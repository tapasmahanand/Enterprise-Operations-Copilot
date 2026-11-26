"""D59: the composer's FACTS state the draft workflow as a fact when a draft exists, and say plainly
when none does, so "click Send to supervisor" is grounded exactly when it is true."""
from kestrel.graphs.chat import COMPOSER_SYSTEM, _facts

SUP = {"intent": "cash_withdrawal_charge", "route": "rag"}


def test_no_draft_is_stated_with_its_reason():
    f = _facts({"sup": SUP, "no_draft_reason": "caller not verified"})
    assert "no draft exists in this run (caller not verified)" in f and "nothing for the agent to review" in f


def test_no_draft_is_stated_even_without_a_reason():
    assert "no draft exists in this run;" in _facts({"sup": SUP})


def test_a_draft_comes_with_the_send_to_supervisor_step():
    d = {"draft_id": "DRF-1", "kind": "fee_reversal", "amount_inr": 23, "approver_role": "team_lead"}
    f = _facts({"sup": {**SUP, "route": "action"}, "draft": d})
    assert "draft DRF-1" in f and '"Send to supervisor"' in f and "no draft exists" not in f


def test_composer_is_told_not_to_mention_a_draft_that_does_not_exist():
    assert "not even conditionally" in COMPOSER_SYSTEM
