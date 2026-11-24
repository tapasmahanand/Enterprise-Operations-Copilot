from kestrel.guards.output import check_output

R = {"KB-POL-LIAB-A:1", "KB-SCRIPT-FRAUD:0"}


def test_clean_answer_passes():
    r = check_output("Zero liability applies [KB-POL-LIAB-A:1]; shadow credit due 2026-12-07.", R,
                     approval_exists=False, allowed_numbers={"2026", "12", "07"})
    assert r["verdict"] == "allow" and r["citations"] == ["KB-POL-LIAB-A:1"]


def test_invented_citation_blocked():
    r = check_output("See [KB-POL-FEES:0].", R, approval_exists=False)
    assert r["verdict"] == "block" and "not retrieved" in r["violations"][0]


def test_unapproved_promise_blocked():
    r = check_output("Tell her she will definitely be refunded [KB-POL-LIAB-A:1].", R, approval_exists=False)
    assert r["verdict"] == "block" and "promise" in r["violations"][0]


def test_invented_amount_blocked():
    r = check_output("Customer pays ₹5,000 [KB-POL-LIAB-A:1].", R, approval_exists=False, allowed_numbers={"18,450"})
    assert r["verdict"] == "block" and "5,000" in r["violations"][0]


def test_rule_and_case_ids_are_not_treated_as_numbers():
    r = check_output("Per R17-6ii and draft DRF-0B1DC13D [KB-POL-LIAB-A:1].", R, approval_exists=False, allowed_numbers=set())
    assert r["verdict"] == "allow"


def test_unmasked_card_blocked():
    r = check_output("Card 4111 1111 1111 1111 is blocked.", R, approval_exists=False)
    assert r["verdict"] == "block"
