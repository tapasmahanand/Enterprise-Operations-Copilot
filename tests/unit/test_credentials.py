"""D53: an OTP value in the note is masked, but only sharing language decides liability."""

import pytest

from kestrel.guards.credentials import credential_sharing
from kestrel.guards.pii import mask_pii

CASES = [
    # The reported bug: an OTP value plus an explicit denial used to flip liability to the customer.
    ("got an SMS with OTP 482913 but says she did not share it", False),
    ("Customer says there is a ₹18,450 card payment she never made on card ending 4821. "
     "She got an SMS with OTP 482913 but says she did not share it.", False),
    ("She received an OTP. She says she never shared it with anyone.", False),
    ("No OTP was shared, she is sure.", False),
    ("Customer denies sharing her PIN or OTP", False),
    # The real shared-credential cases must still flip it (eval S21, H03).
    ("Customer never made the ₹18,450 card payment on card ending 4821 but admits she read out the OTP "
     "to a caller claiming to be from the bank.", True),
    ("A caller pretending to be from the bank got the customer to share an OTP, then ₹4,999 went out "
     "on card ending 7710 at Urban Threads.", True),
    ("I told them the OTP 482913", True),
    ("She entered her card number on a website that looked like the bank", True),
    ("fraudster: 'just tell me the OTP madam'. Customer gave it.", True),
    ("Customer shared the OTP with a caller; fraudster said they were from Kestrel", True),
    # Unknown: the agent is asked to confirm (D42).
    ("the pin is 1234", None),
    ("Customer told me she got an OTP she didn't request", None),
    ("Customer said the OTP never arrived", None),
    ("The caller told her to share the OTP but she refused", None),
    ("Customer read out the OTP to me for verification", None),
    ("Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821", None),
]


@pytest.mark.parametrize("note,expected", CASES)
def test_credential_sharing(note, expected):
    # The guard reads the masked note, exactly as the chat graph does.
    assert credential_sharing(mask_pii(note).text) is expected


def test_otp_is_still_masked_when_not_shared():
    m = mask_pii("got an SMS with OTP 482913 but says she did not share it")
    assert "482913" not in m.text and m.found.get("secret") == 1
