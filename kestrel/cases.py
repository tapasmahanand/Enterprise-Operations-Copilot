"""Case lifecycle: draft → (agent submits) case + approval → (supervisor decides) resume."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

from kestrel.db import connect
from kestrel.graphs.case import resume_case, start_case


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, existing: dict[str, Any]):
        super().__init__("already decided")
        self.existing = existing


def submit_draft(draft_id: str) -> dict[str, Any]:
    """Agent's "Send to supervisor". Idempotent: a second click returns the same case."""
    with connect() as conn:
        row = conn.execute("SELECT session_id, action, evidence, calc, case_id FROM drafts WHERE draft_id = %s FOR UPDATE",
                           (draft_id,)).fetchone()
        if row is None:
            raise NotFound(draft_id)
        session_id, action, evidence, calc, case_id = row
        if case_id:
            conn.commit()
            return get_case(case_id)
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        approval_id = f"APR-{case_id[5:]}"
        due = (calc or {}).get("shadow_credit_due") or (calc or {}).get("resolution_due")
        conn.execute("INSERT INTO sessions (session_id, user_id) VALUES (%s, 'agent') ON CONFLICT DO NOTHING", (session_id,))
        conn.execute(
            """INSERT INTO cases (case_id, session_id, status, risk_level, evidence, recommended_action, due_at, draft_id, approver_role)
               VALUES (%s, %s, 'pending_approval', %s, %s, %s, %s, %s, %s)""",
            (case_id, session_id, action.get("risk_level", "high"), Jsonb(evidence), Jsonb({**action, "calc": calc}),
             due, draft_id, action.get("approver_role")))
        conn.execute(
            """INSERT INTO approvals (approval_id, case_id, thread_id, state, approver_role, action_payload)
               VALUES (%s, %s, %s, 'pending', %s, %s)""",
            (approval_id, case_id, case_id, action.get("approver_role"), Jsonb(action)))
        conn.execute("UPDATE drafts SET case_id = %s WHERE draft_id = %s", (case_id, draft_id))
        conn.commit()
    # Outside the transaction: if we die here, reconcile() starts the graph on boot.
    start_case(case_id, approval_id, action)
    return get_case(case_id)


def decide(approval_id: str, decision: str, approver_id: str, reason: str | None) -> dict[str, Any]:
    """Record the decision atomically (only one can win), then resume the case graph.
    A second decision gets Conflict with the first one and changes nothing."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    new_state = "approved" if decision == "approve" else "rejected"
    with connect() as conn:
        won = conn.execute(
            """UPDATE approvals SET state = %s, approver_id = %s, reason = %s, decided_at = now()
               WHERE approval_id = %s AND state = 'pending' RETURNING case_id""",
            (new_state, approver_id, reason, approval_id)).fetchone()
        if won is None:
            ex = conn.execute("SELECT case_id, state, approver_id, reason, decided_at FROM approvals WHERE approval_id = %s",
                              (approval_id,)).fetchone()
            conn.commit()
            if ex is None:
                raise NotFound(approval_id)
            raise Conflict({"approval_id": approval_id, "case_id": ex[0], "state": ex[1], "approver_id": ex[2],
                            "reason": ex[3], "decided_at": ex[4].isoformat() if ex[4] else None})
        conn.commit()
    case_id = won[0]
    resume_case(case_id, {"decision": decision, "reason": reason, "approver_id": approver_id})
    return get_case(case_id)


def get_case(case_id: str) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute(
            """SELECT c.case_id, c.status, c.risk_level, c.evidence, c.recommended_action, c.due_at, c.result,
                      c.approver_role, c.created_at, c.updated_at, a.approval_id, a.state, a.approver_id, a.reason
               FROM cases c LEFT JOIN approvals a USING (case_id) WHERE c.case_id = %s""", (case_id,)).fetchone()
    if r is None:
        raise NotFound(case_id)
    return {"case_id": r[0], "status": r[1], "risk_level": r[2], "evidence": r[3], "recommended_action": r[4],
            "due_at": r[5].isoformat() if r[5] else None, "result": r[6], "approver_role": r[7],
            "created_at": r[8].isoformat(), "updated_at": r[9].isoformat(),
            "approval": {"approval_id": r[10], "state": r[11], "approver_id": r[12], "reason": r[13]}}


def pending_approvals() -> list[dict[str, Any]]:
    """Supervisor queue, most urgent first (earliest RBI-derived due date)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.approval_id, a.case_id, a.approver_role, a.action_payload, c.due_at, c.evidence,
                      c.recommended_action, a.created_at, c.status
               FROM approvals a JOIN cases c USING (case_id)
               WHERE a.state = 'pending' ORDER BY c.due_at NULLS LAST, a.created_at""").fetchall()
    return [{"approval_id": r[0], "case_id": r[1], "approver_role": r[2], "action": r[3],
             "due_at": r[4].isoformat() if r[4] else None, "evidence": r[5], "calc": (r[6] or {}).get("calc"),
             "created_at": r[7].isoformat(), "case_status": r[8]} for r in rows]


def create_draft(session_id: str, run_id: str, action: dict, evidence: list, calc: dict | None) -> str:
    draft_id = f"DRF-{uuid.uuid4().hex[:8].upper()}"
    with connect() as conn:
        conn.execute("INSERT INTO drafts (draft_id, session_id, run_id, action, evidence, calc) VALUES (%s,%s,%s,%s,%s,%s)",
                     (draft_id, session_id, run_id, Jsonb(action), Jsonb(evidence), Jsonb(calc) if calc else None))
        conn.commit()
    return draft_id
