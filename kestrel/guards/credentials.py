"""Did the customer share an OTP/PIN/card details? Decides the liability input `fault` (D42, D53).

Seeing a code in the note is not the same as the customer having shared it: "got an SMS
with OTP 482913 but says she did not share it" contains an OTP and an explicit denial.
Until D53 any OTP value in the note set the flag, the calculator moved liability to the
customer and no credit was drafted. The value is still masked (pii.py); this module only
reads the words around it.

Verdict, per note:
  True   sharing language, not negated ("admits she read out the OTP to a caller")
  False  only negated sharing language ("says she did not share it", "never gave her PIN")
  None   neither, or both ("the caller told her to share the OTP but she refused"): the agent
         is asked to confirm, which is the existing behaviour for unknown (D42).

Rule-based on purpose: this input moves money, so it has to be explainable and unit-tested.
Limitation: English only, sentence-level. A note in Hinglish, or one that spreads the
statement over several sentences without naming the credential, lands on None."""

from __future__ import annotations

import re

_CRED = re.compile(r"\b(otp|otps|pin|cvv|password|passcode|card (?:details|number)|one[- ]time (?:code|password))\b", re.I)
_VERB = re.compile(r"\b(shar(?:e|ed|es|ing)|g[ai]ve|given|giving|t[eo]ll?s?|told|telling|disclos(?:e|ed|ing)|"
                   r"read(?:s)? (?:it |them )?out|enter(?:ed|s|ing)?|typ(?:e|ed|es|ing)|reveal(?:ed|s|ing)?|"
                   r"provid(?:e|ed|es|ing)|passed (?:it |them )?on|forward(?:ed)?)\b", re.I)
_PRONOUN = re.compile(r"^\W*(it|them|the code|the codes)\b", re.I)
# A negator up to three words before the verb: "did not share", "never gave", "no OTP was shared",
# "denies sharing", "hasn't given".
_NEG = re.compile(r"\b(not|never|no|didn'?t|did ?nt|hasn'?t|hadn'?t|wasn'?t|isn'?t|won'?t|denie[sd]|deny|denying|"
                  r"nobody|none|neither|nor|without)\b", re.I)
# Refusing to the fraudster is a denial even with an affirmative verb nearby ("told her to share
# the OTP but she refused").
_REFUSED = re.compile(r"\b(refused|declined|hung up|did not fall for|didn'?t fall for)\b", re.I)
# Reading a code out to the bank itself is not negligence: "read out the OTP to me for verification".
_TO_BANK = re.compile(r"\b(to me|to us|to the agent|for verification|to verify (?:her|him|them|the caller)|on this call)\b", re.I)
_RECEIVED = re.compile(r"\b(got|get|gets|getting|received|receives|receiving|came|arrived|had|has)\b", re.I)
_SENTENCE = re.compile(r"[.;!?\n]+")


def _negated(sentence: str, verb_start: int) -> bool:
    before = sentence[:verb_start].split()[-3:]
    return any(_NEG.fullmatch(w.strip(",:'\"()")) for w in before)


def _about_credential(sentence: str, m: re.Match) -> bool:
    """The verb is about a credential only if one is right next to it: in the next five words
    ("shared the OTP", "told the caller her PIN"), as "it"/"them" ("did not share it"), or in
    the four words before it ("no OTP was shared"). "Customer told me she got an OTP" is not."""
    after = " ".join(sentence[m.end():].split()[:5])
    if (cred := _CRED.search(after)):
        # "told me she got an OTP": a second verb between them means the credential belongs to it.
        if _RECEIVED.search(after[:cred.start()]):
            after = ""
    before = " ".join(sentence[:m.start()].split()[-4:])
    return bool(_CRED.search(after) or _PRONOUN.search(sentence[m.end():]) or _CRED.search(before))


def credential_sharing(text: str) -> bool | None:
    """True / False / None as described in the module docstring."""
    affirmed = denied = False
    prev_had_cred = False
    for sentence in _SENTENCE.split(text):
        has_cred = bool(_CRED.search(sentence))
        # "She got an OTP. She says she did not share it." -- the pronoun refers back one sentence.
        if not has_cred and not (prev_had_cred and re.search(r"\b(it|them)\b", sentence, re.I)):
            prev_had_cred = False
            continue
        prev_had_cred = has_cred
        if _REFUSED.search(sentence):
            denied = True
        for m in _VERB.finditer(sentence):
            if not _about_credential(sentence, m):
                continue
            if _negated(sentence, m.start()):
                denied = True
            elif not _TO_BANK.search(sentence):
                affirmed = True
    if affirmed and denied:
        return None
    if affirmed:
        return True
    if denied:
        return False
    return None
