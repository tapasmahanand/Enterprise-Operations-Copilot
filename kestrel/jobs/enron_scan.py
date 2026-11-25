"""Batch compliance scan over the Enron email sample (async workflow).

Flags personal data and a few policy-breach patterns a bank's compliance team scans for.
Rules are regex, each with an id, so every flag is explainable. Snippets in the result
are masked; the job never stores an unmasked match. Deterministic, so a retried or
re-claimed job produces the same result (at-least-once delivery is safe)."""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from kestrel.config import get_settings
from kestrel.guards.pii import mask_pii

_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_US_PHONE = re.compile(r"(?<!\d)\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")

RULES: list[tuple[str, str, re.Pattern]] = [
    ("CS-PII-SSN", "US social security number", _SSN),
    ("CS-PII-ACCT", "bank account or card number in text",
     re.compile(r"\b(?:account|acct|a/c|card)\s*(?:no\.?|number|#)?\s*:?\s*\d{6,}", re.I)),
    ("CS-CRED", "credentials shared in email",
     re.compile(r"\b(?:my|the|your)\s+(?:password|passcode|pin)\s+(?:is|=|:)\s*\S+|\bpassword\s*:\s*\S+", re.I)),
    ("CS-DESTROY", "instruction to delete or destroy records",
     re.compile(r"\b(?:delete|destroy|shred)\b.{0,25}\b(?:this|these|the|all)\b.{0,15}\b(?:e-?mails?|messages?|documents?|files?|records?)", re.I)),
]


# Legal footers ("if you are not the intended recipient … delete all copies") are not
# instructions to destroy records; ignore matches inside them.
_DISCLAIMER = re.compile(r"intended recipient|in error|notify (?:us|the sender)|copies of (?:the|this)|anyone\.?\s+in such case", re.I)


_ADDR = re.compile(r"[\w.+'-]+@([\w-]+(?:\.[\w-]+)+)")
_CONF = re.compile(r"confidential|privileged", re.I)


def _external_domains(msg: dict) -> list[str]:
    # To and Cc are joined with a space and some lists are space-separated, so split on
    # addresses, not commas (a comma split hid "x@caiso.com y@enron.com" as internal).
    return [d.lower() for d in _ADDR.findall(msg.get("recipients") or "") if not d.lower().endswith("enron.com")]


def _external_confidential(msg: dict) -> bool:
    # Subject or opening lines only: most bodies end in a "confidential" legal footer.
    # A marking inside a footer ("may contain confidential information...") doesn't count.
    if not _external_domains(msg):
        return False
    if _CONF.search(msg.get("subject") or ""):
        return True
    head = (msg.get("body") or "")[:400]
    return any(not _DISCLAIMER_OR_FOOTER.search(head[max(0, m.start() - 120):m.end() + 120]) for m in _CONF.finditer(head))


_DISCLAIMER_OR_FOOTER = re.compile(r"intended recipient|in error|may contain|sole use|protected by the", re.I)

_CARD_RUN = re.compile(r"(?<![\w/=.-])(?:\d[ -]?){12,18}\d(?![\w/.-])")
_AADHAAR_RUN = re.compile(r"(?<![\w/=.-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\w/.-])")
_PAN_RUN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_VD = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],
       [5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_VP = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],[9,4,5,3,1,2,6,8,7,0],
       [4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]


def _verhoeff(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def _kb_ids(body: str) -> list[str]:
    """Card (Luhn), Aadhaar (Verhoeff) and PAN numbers that pass their checksums and are not
    part of a URL, id or MIME boundary. Stricter than chat masking on purpose: masking can
    over-mask cheaply, but every scan flag costs a reviewer's time."""
    from kestrel.guards.pii import _luhn
    found = set()
    for m in _CARD_RUN.finditer(body):
        d = re.sub(r"\D", "", m.group(0))
        if 13 <= len(d) <= 19 and _luhn(d):
            found.add("card")
    for m in _AADHAAR_RUN.finditer(body):
        if _verhoeff(re.sub(r"\D", "", m.group(0))):
            found.add("aadhaar")
    if _PAN_RUN.search(body):
        found.add("pan")
    return sorted(found)


_LABELLED_DIGITS = re.compile(r"(\b(?:account|acct|a/c|card)\s*(?:no\.?|number|#)?\s*:?\s*)\d*(\d{4})", re.I)


def _mask(text: str) -> str:
    text = _US_PHONE.sub("XXX-XXX-XXXX", _SSN.sub("XXX-XX-XXXX", mask_pii(text).text))
    # CS-PII-ACCT matches 6+ digits after a label; mask_pii has no rule for short account numbers.
    return _LABELLED_DIGITS.sub(lambda m: m.group(1) + "XXXX" + m.group(2), text)


def scan_message(msg: dict) -> list[dict[str, str]]:
    body = msg.get("body", "")
    flags = []
    for rule_id, label, pat in RULES:
        m = next((m for m in pat.finditer(body)
                  if not (rule_id == "CS-DESTROY" and _DISCLAIMER.search(body[max(0, m.start() - 200):m.end() + 60]))),
                 None)
        if m:
            lo = max(0, m.start() - 60)
            flags.append({"rule_id": rule_id, "label": label, "snippet": _mask(body[lo:m.end() + 60])})
    if kb := _kb_ids(body):
        flags.append({"rule_id": "CS-PII-KB", "label": "card/Aadhaar/PAN number, checksum-valid (KB-GUIDE-PII)",
                      "snippet": ", ".join(kb)})
    if _external_confidential(msg):
        flags.append({"rule_id": "CS-EXT-CONF", "label": "'confidential' content sent outside the firm", "snippet": ""})
    return flags


def run(payload: dict[str, Any], report: Callable[[dict], None]) -> dict[str, Any]:
    path = Path(payload.get("path") or get_settings().enron_sample)
    limit = int(payload.get("limit") or 10_000)
    counts: Counter = Counter()
    flagged: list[dict] = []
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if n >= limit:
                break
            msg = json.loads(line)
            n += 1
            flags = scan_message(msg)
            for fl in flags:
                counts[fl["rule_id"]] += 1
            if flags:
                flagged.append({"message_id": msg["message_id"], "date": msg.get("date"), "folder": msg.get("folder"),
                                "subject": _mask(msg.get("subject") or "")[:120], "flags": flags})
            if n % 250 == 0:
                report({"processed": n, "flagged": len(flagged)})
    report({"processed": n, "flagged": len(flagged)})
    return {"scanned": n, "flagged_messages": len(flagged), "flags_by_rule": dict(counts),
            "rules": {r[0]: r[1] for r in RULES} | {"CS-PII-KB": "card/Aadhaar/PAN pattern",
                                                    "CS-EXT-CONF": "'confidential' sent outside the firm"},
            "sample": flagged[:50], "note": "Heuristic flags for human review, not findings."}
