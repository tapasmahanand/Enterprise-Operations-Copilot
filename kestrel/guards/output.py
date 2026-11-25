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
# A promise phrase preceded (same clause, a few words back) by a negation is the opposite of a
# promise: "do not guarantee the refund", "you can't guarantee it today". Eval run 4: all three
# answers withheld for 'guarantee' (H07) were telling the agent NOT to guarantee (D47).
_NEGATED = re.compile(r"(\b(not|never|cannot|can't|can’t|don't|don’t|won't|won’t|shouldn't|shouldn’t|unable to)\b"
                      r"(\s+\w+){0,3}|\bno)\s*$", re.I)   # "no" only directly before: "no guarantee"


def _unnegated_promise(answer: str) -> re.Match | None:
    for m in _PROMISE.finditer(answer):
        before = answer[max(0, m.start() - 60):m.start()]
        before = re.split(r"[.;:!?\n]", before)[-1]          # same clause only
        if not _NEGATED.search(before):
            return m
    return None


def check_output(answer: str, retrieved: set[str], *, approval_exists: bool,
                 allowed_numbers: set[str] | None = None) -> dict:
    cited = list(dict.fromkeys(_CITE.findall(answer)))
    violations = []
    bad = [c for c in cited if c not in retrieved]
    if bad:
        violations.append(f"cites chunks that were not retrieved: {', '.join(bad)}")
    if has_unmasked_pii(answer):
        violations.append("contains unmasked personal data")
    if not approval_exists and (m := _unnegated_promise(answer)):
        violations.append(f"unapproved promise: '{m.group(0)}'")
    if allowed_numbers is not None:
        stripped = _CITE.sub("", answer)
        stripped = re.sub(r"\bR\d{2}-[\w-]+|\bKB-[\w-]+|\b(?:DRF|CASE|APR|TCK|T)-[\w-]+", "", stripped)
        allowed_norm = {a.replace(",", "") for a in allowed_numbers}
        invented = [n for n in _NUM.findall(stripped) if n.replace(",", "") not in allowed_norm and len(n.replace(",", "")) > 1]
        if invented:
            violations.append(f"numbers not in facts or sources: {', '.join(sorted(set(invented)))}")
    return {"verdict": "block" if violations else "allow", "violations": violations, "citations": [c for c in cited if c in retrieved]}
