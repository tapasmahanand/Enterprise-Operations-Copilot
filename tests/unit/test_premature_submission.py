"""D61: claims that the request is already with a supervisor, before the agent clicks Send."""
import pytest

from kestrel.guards.output import premature_submission


@pytest.mark.parametrize("text", [
    "Tell the customer: I've raised a request to credit the duplicate amount; it needs approval.",
    "you can only say the credit has been sent for approval, not guaranteed",
    "I've raised this with my supervisor for approval.",
    "The refund request is now with the supervisor.",
    "Your request is pending approval.",
])
def test_flags_claims_of_submission(text):
    assert premature_submission(text)


@pytest.mark.parametrize("text", [
    'Review draft DRF-1 and click "Send to supervisor"; nothing is submitted until you do.',
    "No action can be drafted until verification is raised to level 2; nothing has been drafted or sent.",
    "The attempt has been logged. Please rephrase.",
    "It has not been sent yet; click Send to supervisor.",
    "This needs my supervisor's approval, and I'm sending it to them now.",
])
def test_ignores_correct_or_negated_statements(text):
    assert premature_submission(text) is None


def test_curly_apostrophes_and_second_person():
    assert premature_submission("tell them you’ve raised a request to credit the duplicate amount")
    assert premature_submission("“I’ve raised a request to credit the duplicate amount.”")
