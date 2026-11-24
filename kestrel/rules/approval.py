"""Approval matrix: decides whether an action needs approval, who approves, or refusal.

The Supervisor LLM proposes; this module decides. The stricter answer always wins.
"""

from __future__ import annotations

from pydantic import BaseModel

from kestrel.rules.loader import Rules


class ApprovalRequirement(BaseModel):
    action: str
    needs_approval: bool
    approver_role: str | None = None
    refused: bool = False
    refusal_rule: str | None = None
    reasons: list[str] = []


def approval_requirement(
    action: str,
    amount_inr: int | None,
    rules: Rules,
    *,
    repeat_dispute: bool = False,
    identity_verified: bool = True,
) -> ApprovalRequirement:
    matrix = rules.approval_matrix
    spec = matrix["actions"].get(action)
    if spec is None:
        raise ValueError(f"unknown action {action!r}")

    if not identity_verified:
        return ApprovalRequirement(action=action, needs_approval=False, refused=True,
                                   refusal_rule="KB-REF-1", reasons=["identity_not_verified"])

    if not spec["needs_approval"]:
        return ApprovalRequirement(action=action, needs_approval=False)

    reasons = ["matrix"]
    role = None
    if spec["tiered"]:
        if amount_inr is None:
            raise ValueError(f"{action} needs an amount to pick the approver tier")
        tiers = matrix["tiers"]
        idx = next(i for i, t in enumerate(tiers) if t["max_inr"] is None or amount_inr <= t["max_inr"])
        if repeat_dispute:
            idx = min(idx + 1, len(tiers) - 1)
            reasons.append("repeat_dispute")
        role = tiers[idx]["role"]
    return ApprovalRequirement(action=action, needs_approval=True, approver_role=role, reasons=reasons)
