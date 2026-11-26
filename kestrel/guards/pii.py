"""PII masking, per KB-GUIDE-PII. Runs before any text is stored, logged or sent to a model.

Regex, not an NER model: the formats are fixed (card, Aadhaar, PAN, mobile, email,
OTP/PIN/CVV), and a regex is fast, auditable and unit-testable. Names are not masked
(a known limitation, stated in the write-up)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?(\d{4})(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}(\d{4})[A-Z]\b")
_MOBILE = re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{4}[ -]?\d{1}(\d{4})(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")
_SECRET = re.compile(r"\b(otp|pin|cvv|password|passcode)\b(\s*(?:is|was|:|=)?\s*)(\d{3,8})", re.I)


@dataclass
class MaskResult:
    text: str
    found: dict[str, int] = field(default_factory=dict)
    card_last4: list[str] = field(default_factory=list)
    # An OTP/PIN/CVV value was in the text (and is now masked). This says nothing about whether the
    # customer shared it with anyone: guards/credentials.py decides that from the words (D53).
    secret_present: bool = False

    @property
    def verdict(self) -> str:
        return "redact" if self.found else "allow"


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def mask_pii(text: str) -> MaskResult:
    r = MaskResult(text=text)

    def bump(kind: str) -> None:
        r.found[kind] = r.found.get(kind, 0) + 1

    def secret(m: re.Match) -> str:
        bump("secret"); r.secret_present = True
        return f"{m.group(1)}{m.group(2)}[REDACTED]"

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 12:          # leave for the Aadhaar pass
            return m.group(0)
        if not 13 <= len(digits) <= 19:
            return m.group(0)
        # Mask any 13-19 digit run, Luhn-valid or not: a mistyped card number is still
        # a card number, and leaving it would let the Aadhaar pass mask only part of it.
        bump("card" if _luhn(digits) else "card_unverified"); r.card_last4.append(digits[-4:])
        return f"XXXX-{digits[-4:]}"

    def aadhaar(m: re.Match) -> str:
        bump("aadhaar"); return f"XXXX-XXXX-{m.group(1)}"

    def pan(m: re.Match) -> str:
        bump("pan"); return f"XXXXX{m.group(1)}X"

    def mobile(m: re.Match) -> str:
        bump("mobile"); return f"XXXXXX{m.group(1)}"

    def email(m: re.Match) -> str:
        bump("email"); return f"[email at {m.group(1)}]"

    t = _SECRET.sub(secret, text)
    t = _CARD.sub(card, t)
    t = _AADHAAR.sub(aadhaar, t)
    t = _PAN.sub(pan, t)
    t = _MOBILE.sub(mobile, t)
    t = _EMAIL.sub(email, t)
    r.text = t
    return r


def has_unmasked_pii(text: str) -> bool:
    return mask_pii(text).text != text
