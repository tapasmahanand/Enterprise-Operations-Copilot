"""Case lifecycle: draft → (agent submits) case + approval → (supervisor decides) resume."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from kestrel import ledger, outbox
from kestrel.auth import audit
from kestrel.db import connect
from kestrel.faults import crash_point
from kestrel.graphs.case import decision_event, start_case
from kestrel.tracing import trace_id_for

if TYPE_CHECKING:
    from kestrel.auth import User


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


TIER_RANK = {"team_lead": 1, "cards_team_lead": 1, "payments_ops_lead": 1, "ops_manager": 2, "regional_ops_head": 3}


class Conflict(Exception):
    def __init__(self, existing: dict[str, Any]):
        super().__init__("already decided")
        self.existing = existing


class Stale(Exception):
    """The supervisor decided on a version of the case that is no longer the current one."""
    def __init__(self, expected: dict[str, Any], actual: dict[str, Any]):
        super().__init__("case changed since it was shown")
        self.expected, self.actual = expected, actual


log = logging.getLogger("cases")


def submit_draft(draft_id: str, submitted_by: User | None = None) -> dict[str, Any]:
    """Agent's "Send to supervisor". Idempotent: a second click returns the same case.
    Through the API, `submitted_by` is the signed-in agent, who must be the one whose chat
    run created the draft (D50). Direct calls (tests, scripts) pass None."""
    with connect() as conn:
        row = conn.execute("SELECT session_id, action, evidence, calc, case_id, created_by FROM drafts"
                           " WHERE draft_id = %s FOR UPDATE", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(draft_id)
        session_id, action, evidence, calc, case_id, created_by = row
        if submitted_by is not None and created_by != submitted_by.user_id:
            audit(conn, submitted_by, "draft_submit_denied", "draft", draft_id, {"created_by": created_by})
            conn.commit()
            raise Forbidden(f"draft {draft_id} was not drafted in your session")
        if case_id:
            conn.commit()
            return get_case(case_id)
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        approval_id = f"APR-{case_id[5:]}"
        due = (calc or {}).get("shadow_credit_due") or (calc or {}).get("resolution_due")
        who = submitted_by.user_id if submitted_by else None
        conn.execute("INSERT INTO sessions (session_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (session_id, who or "agent"))
        conn.execute(
            """INSERT INTO cases (case_id, session_id, status, risk_level, evidence, recommended_action, due_at, draft_id,
                                  approver_role, trace_id, submitted_by)
               VALUES (%s, %s, 'pending_approval', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (case_id, session_id, action.get("risk_level", "high"), Jsonb(evidence), Jsonb({**action, "calc": calc}),
             due, draft_id, action.get("approver_role"), trace_id_for(case_id), who))
        conn.execute(
            """INSERT INTO approvals (approval_id, case_id, thread_id, state, approver_role, action_payload)
               VALUES (%s, %s, %s, 'pending', %s, %s)""",
            (approval_id, case_id, case_id, action.get("approver_role"), Jsonb(action)))
        conn.execute("UPDATE drafts SET case_id = %s WHERE draft_id = %s", (case_id, draft_id))
        audit(conn, submitted_by, "draft_submitted", "case", case_id,
              {"draft_id": draft_id, "approval_id": approval_id, "action": action.get("action"),
               "kind": action.get("kind"), "amount_inr": action.get("amount_inr"),
               "approver_role": action.get("approver_role"), "idv_level": action.get("idv_level")})
        conn.commit()
    # Outside the transaction: if we die here, reconcile() starts the graph on boot.
    start_case(case_id, approval_id, action)
    return get_case(case_id)


def decide(approval_id: str, decision: str, approver_id: str | None, reason: str | None,
           decided_by: User | None = None, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the decision, bind it to the exact transaction, amount and draft version, and
    write its outbox event: one transaction (D55). Only one decision can win; a second gets
    Conflict with the first and changes nothing. `expected` ({txn_id, amount_inr,
    draft_version}, any subset) is what the supervisor's screen showed; a mismatch is Stale.

    Execution is not part of the decision: it is driven by the outbox event. By default the
    API dispatches that event right after the commit so the supervisor sees the outcome; if
    the process dies first, the worker or reconcile() picks it up.

    Through the API, `decided_by` is the signed-in supervisor and the approver identity is
    their org_directory person, whatever `approver_id` says (D50). The supervisor also may not
    approve a case they submitted themselves (separation of duties)."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    if decided_by is not None:
        approver_id = decided_by.person_id
    new_state = "approved" if decision == "approve" else "rejected"
    with connect() as conn:
        need = conn.execute("""SELECT a.approver_role, c.submitted_by FROM approvals a JOIN cases c USING (case_id)
                               WHERE a.approval_id = %s""", (approval_id,)).fetchone()
        who = conn.execute("SELECT role FROM org_directory WHERE person_id = %s", (approver_id,)).fetchone()
        if need is None:
            raise NotFound(approval_id)
        denied = None
        if decided_by is not None and need[1] is not None and need[1] == decided_by.user_id:
            denied = f"{decided_by.user_id} submitted this case and cannot also decide it"
        # Matrix tier check (KB-MATRIX): the approver must be at or above the required tier.
        elif who is None or TIER_RANK.get(who[0], 0) < TIER_RANK.get(need[0] or "team_lead", 1):
            denied = f"{approver_id} ({who[0] if who else 'unknown'}) cannot approve a case that needs {need[0]}"
        if denied:
            audit(conn, decided_by, "approval_denied", "approval", approval_id,
                  {"decision": decision, "approver_id": approver_id, "needs": need[0], "why": denied})
            conn.commit()
            raise Forbidden(denied)
        bound = ledger.binding_for(conn, approval_id)
        if expected:
            diff = {k: v for k, v in expected.items() if v is not None and bound.get(k) != v}
            if diff:
                audit(conn, decided_by, "approval_stale", "approval", approval_id,
                      {"expected": expected, "actual": {k: bound.get(k) for k in diff}})
                conn.commit()
                raise Stale(expected, {k: bound.get(k) for k in diff})
        won = conn.execute(
            """UPDATE approvals SET state = %s, approver_id = %s, reason = %s, decided_at = now(), decided_by = %s,
                      bound_txn_id = %s, bound_amount_inr = %s, bound_draft_version = %s, bound_action_hash = %s
               WHERE approval_id = %s AND state = 'pending' RETURNING case_id, action_payload""",
            (new_state, approver_id, reason, decided_by.user_id if decided_by else None,
             bound["txn_id"], bound["amount_inr"], bound["draft_version"], bound["action_hash"],
             approval_id)).fetchone()
        if won is None:
            ex = conn.execute("SELECT case_id, state, approver_id, reason, decided_at FROM approvals WHERE approval_id = %s",
                              (approval_id,)).fetchone()
            audit(conn, decided_by, "approval_conflict", "approval", approval_id,
                  {"attempted": decision, "existing_state": ex[1] if ex else None})
            conn.commit()
            if ex is None:
                raise NotFound(approval_id)
            raise Conflict({"approval_id": approval_id, "case_id": ex[0], "state": ex[1], "approver_id": ex[2],
                            "reason": ex[3], "decided_at": ex[4].isoformat() if ex[4] else None})
        event_id = decision_event(conn, won[0], approval_id, new_state, reason, approver_id, won[1])
        audit(conn, decided_by, "approval_decided", "approval", approval_id,
              {"case_id": won[0], "decision": decision, "approver_id": approver_id, "approver_tier": who[0],
               "needs": need[0], "reason": reason, "bound": bound, "event_id": event_id})
        conn.commit()
    case_id = won[0]
    crash_point("after_approval_commit")   # recovery test hook: decided, event not yet handled
    if os.environ.get("OUTBOX_INLINE_DISPATCH", "1") == "1":
        try:
            outbox.dispatch_due(limit=1, event_id=event_id)
        except Exception:  # noqa: BLE001 - the decision is committed; the worker retries the event
            log.exception("inline dispatch of %s failed; left for the worker", event_id)
    return get_case(case_id)


def get_case(case_id: str) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute(
            """SELECT c.case_id, c.status, c.risk_level, c.evidence, c.recommended_action, c.due_at, c.result,
                      c.approver_role, c.created_at, c.updated_at, a.approval_id, a.state, a.approver_id, a.reason,
                      c.submitted_by, a.decided_by
               FROM cases c LEFT JOIN approvals a USING (case_id) WHERE c.case_id = %s""", (case_id,)).fetchone()
    if r is None:
        raise NotFound(case_id)
    return {"case_id": r[0], "status": r[1], "risk_level": r[2], "evidence": r[3], "recommended_action": r[4],
            "due_at": r[5].isoformat() if r[5] else None, "result": r[6], "approver_role": r[7],
            "created_at": r[8].isoformat(), "updated_at": r[9].isoformat(),
            "submitted_by": r[14],
            "approval": {"approval_id": r[10], "state": r[11], "approver_id": r[12], "reason": r[13], "decided_by": r[15]}}


def pending_approvals() -> list[dict[str, Any]]:
    """Supervisor queue, most urgent first (earliest RBI-derived due date)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.approval_id, a.case_id, a.approver_role, a.action_payload, c.due_at, c.evidence,
                      c.recommended_action, a.created_at, c.status, d.version
               FROM approvals a JOIN cases c USING (case_id) LEFT JOIN drafts d ON d.draft_id = c.draft_id
               WHERE a.state = 'pending' ORDER BY c.due_at NULLS LAST, a.created_at""").fetchall()
    return [{"approval_id": r[0], "case_id": r[1], "approver_role": r[2], "action": r[3],
             "due_at": r[4].isoformat() if r[4] else None, "evidence": r[5], "calc": (r[6] or {}).get("calc"),
             "created_at": r[7].isoformat(), "case_status": r[8], "draft_version": r[9]} for r in rows]


def create_draft(session_id: str, run_id: str, action: dict, evidence: list, calc: dict | None) -> str:
    draft_id = f"DRF-{uuid.uuid4().hex[:8].upper()}"
    with connect() as conn:
        # created_by: the signed-in user who started the chat run (NULL for runs outside the API, e.g. evals).
        conn.execute("""INSERT INTO drafts (draft_id, session_id, run_id, action, evidence, calc, created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,(SELECT user_id FROM chat_runs WHERE run_id = %s))""",
                     (draft_id, session_id, run_id, Jsonb(action), Jsonb(evidence), Jsonb(calc) if calc else None, run_id))
        conn.commit()
    return draft_id
