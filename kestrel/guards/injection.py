"""Prompt-injection heuristics for agent input (and, in M3, retrieved chunks).

A weighted phrase list, not a classifier: cheap, explainable in an audit, and tested.
Score >= 1.0 blocks. M3 adds an LLM judge for the 0.5–1.0 borderline band."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, float, re.Pattern]] = [
    (name, w, re.compile(p, re.I)) for name, w, p in [
        ("ignore_instructions", 1.0, r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|all|your)\b.{0,20}\b(instructions?|rules|prompt)"),
        ("reveal_prompt", 1.0, r"\b(reveal|show|print|repeat)\b.{0,30}\b(system prompt|hidden instructions|your instructions)"),
        ("role_override", 0.6, r"\byou are now\b|\bact as (an? )?(admin|developer|supervisor)\b|\bdeveloper mode\b"),
        ("approval_bypass", 1.0, r"\b(skip|bypass|without)\b.{0,20}\b(approval|supervisor|verification|kyc)\b"),
        ("fake_authority", 0.5, r"\b(system|admin)\s*:\s"),
        ("addressed_to_model", 0.5, r"\b(assistant|ai|model|copilot)\b,?\s+(when you read|you must|please)\b"),
        ("disable_masking", 1.0, r"\b(skip|disable|turn off|without)\b.{0,15}\bmask"),
        ("exfiltrate_pii", 1.0, r"\b(reveal|show|list|print)\b.{0,30}\b(full|unmasked|complete)\b.{0,20}\b(card|pan|aadhaar|account) numbers?"),
    ]
]


# Policy documents legitimately say "block a card without supervisor approval"; that
# pattern is only an attack signal when it comes from the person typing.
_INPUT_ONLY = {"approval_bypass"}


def injection_score(text: str, source: str = "input") -> tuple[float, list[str]]:
    hits = [(name, w) for name, w, p in _PATTERNS
            if p.search(text) and not (source == "document" and name in _INPUT_ONLY)]
    return sum(w for _, w in hits), [n for n, _ in hits]


_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)


def sanitize_document(text: str) -> tuple[str, list[str]]:
    """Retrieved text is data. Drop each line that looks like an instruction aimed at the
    model (document-mode score >= 0.5, or an HTML comment), keep the rest (D30)."""
    kept, removed = [], []
    # Whole comment spans first (multi-line too); an unclosed "<!--" hides everything after it.
    for m in _HTML_COMMENT.finditer(text):
        removed.append(" ".join(m.group(0).split())[:200])
    text = _HTML_COMMENT.sub("", text)
    for line in text.splitlines():
        if injection_score(line, "document")[0] >= 0.5:
            removed.append(line.strip()[:200])
        else:
            kept.append(line)
    return "\n".join(kept), removed
