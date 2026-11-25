import pytest

from kestrel.guards.pii import has_unmasked_pii, mask_pii


@pytest.mark.parametrize("raw, masked, kind", [
    ("card 4111 1111 1111 1111 was used", "card XXXX-1111 was used", "card"),
    ("card 4111-1111-1111-1111", "card XXXX-1111", "card"),
    ("aadhaar 2345 6789 0123", "aadhaar XXXX-XXXX-0123", "aadhaar"),
    ("PAN is ABCDE1234F", "PAN is XXXXX1234X", "pan"),
    ("call +91 98765 43210", "call XXXXXX3210", "mobile"),
    ("mail priya.s@gmail.com", "mail [email at gmail.com]", "email"),
    ("I told them the OTP 482913", "I told them the OTP [REDACTED]", "secret"),
])
def test_masks_each_kind(raw, masked, kind):
    r = mask_pii(raw)
    assert r.text == masked
    assert r.found == {kind: 1}


def test_card_last4_kept_for_lookup():
    assert mask_pii("my card 4111 1111 1111 1111").card_last4 == ["1111"]


def test_amounts_dates_and_txn_ids_untouched():
    s = "₹18,450 on 2026-11-23, txn T-HERO, ticket TCK-2001, ref 123456"
    assert mask_pii(s).text == s and mask_pii(s).verdict == "allow"


def test_non_luhn_card_length_number_is_still_fully_masked():
    r = mask_pii("card 4111 1111 1111 4821")
    assert r.text == "card XXXX-4821" and r.found == {"card_unverified": 1}


def test_secret_flags_compromise():
    assert mask_pii("the pin is 1234").secret_shared


def test_has_unmasked_pii():
    assert has_unmasked_pii("ABCDE1234F") and not has_unmasked_pii("XXXX-1111")
