"""Output compliance for the Composer's answer.

- every cited chunk id was actually retrieved in this run (no invented citations);
- no unmasked PII;
- no unapproved promise ("will be refunded", "guarantee", ...) unless an approval exists;
- every number in the answer appears in the facts or sources (no invented ₹ or dates)."""

from __future__ import annotations

import re

from kestrel.guards.pii import has_unmasked_pii

_CITE = re.compile(r"\[([A-Z][A-Z0-9-]+:\d+)\]")
_PROMISE = re.compile(
    r"\b(you will (definitely |certainly )?(get|receive) (your|the) (money|refund)|guarantee[ds]?|"
    r"will (definitely|certainly) be (refunded|credited)|has been (refunded|credited)|is approved)\b", re.I)
_NUM = re.compile(r"\d[\d,]*")


def check_output(answer: str, retrieved: set[str], *, approval_exists: bool,
                 allowed_numbers: set[str] | None = None) -> dict:
    cited = list(dict.fromkeys(_CITE.findall(answer)))
    violations = []
    bad = [c for c in cited if c not in retrieved]
    if bad:
        violations.append(f"cites chunks that were not retrieved: {', '.join(bad)}")
    if has_unmasked_pii(answer):
        violations.append("contains unmasked personal data")
    if not approval_exists and (m := _PROMISE.search(answer)):
        violations.append(f"unapproved promise: '{m.group(0)}'")
    if allowed_numbers is not None:
        stripped = _CITE.sub("", answer)
        stripped = re.sub(r"\bR\d{2}-[\w-]+|\bKB-[\w-]+|\b(?:DRF|CASE|APR|TCK|T)-[\w-]+", "", stripped)
        allowed_norm = {a.replace(",", "") for a in allowed_numbers}
        invented = [n for n in _NUM.findall(stripped) if n.replace(",", "") not in allowed_norm and len(n.replace(",", "")) > 1]
        if invented:
            violations.append(f"numbers not in facts or sources: {', '.join(sorted(set(invented)))}")
    return {"verdict": "block" if violations else "allow", "violations": violations, "citations": [c for c in cited if c in retrieved]}
