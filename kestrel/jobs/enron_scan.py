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


def _external_confidential(msg: dict) -> bool:
    # Subject or opening lines only: most bodies end in a "confidential" legal footer.
    text = f"{msg.get('subject', '')} {(msg.get('body') or '')[:400]}".lower()
    rcpts = [r.strip() for r in (msg.get("recipients") or "").split(",") if "@" in r]
    return "confidential" in text and any(not r.lower().endswith("@enron.com") for r in rcpts)


def _mask(text: str) -> str:
    return _US_PHONE.sub("XXX-XXX-XXXX", _SSN.sub("XXX-XX-XXXX", mask_pii(text).text))


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
    pii = mask_pii(body)
    if {"card", "aadhaar", "pan"} & set(pii.found):
        flags.append({"rule_id": "CS-PII-KB", "label": "card/Aadhaar/PAN pattern (KB-GUIDE-PII)",
                      "snippet": ", ".join(sorted(pii.found))})
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
