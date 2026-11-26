"""Case graph: one thread per drafted action (thread_id = case_id).

    approval_gate (interrupt) ──approved──► execute_action ─► notify ─► END
                              └─rejected──► notify ─► END

Restart safety rests on three things:
1. the pause is a Postgres checkpoint, not process memory;
2. every node is safe to re-run (LangGraph re-executes an interrupted node from its
   start on resume, and a crash can land between a side effect and its checkpoint):
   approval rows, credits and notifications are all keyed by case_id;
3. a decision is committed together with an outbox event (D55); the event, not the HTTP
   request, drives the resume, so a crash after the commit is finished by whoever
   dispatches the outbox next (the worker, or `reconcile()` at boot).

Credits post through `ledger.post_credit_and_advance`: credit, receipt, case status and a
CreditPosted event in one transaction, keyed by the approval, after checking that the case
still matches what was approved.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

from kestrel import ledger, outbox
from kestrel.checkpoint import get_checkpointer
from kestrel.db import connect
from kestrel.faults import crash_point
from kestrel.tools.base import ToolError, ToolResult
from kestrel.tools.bank import draft_customer_notification, initiate_chargeback
from kestrel.tracing import root_span, traced

log = logging.getLogger("case_graph")


class CaseState(TypedDict, total=False):
    run_id: str          # = case_id; used by tracing
    thread_id: str
    case_id: str
    approval_id: str
    action: dict[str, Any]
    decision: dict[str, Any]
    credit: dict[str, Any]
    notification: dict[str, Any]
    status: str


def _set_case(case_id: str, status: str, result: dict | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE cases SET status = %s, result = COALESCE(%s, result), updated_at = now() WHERE case_id = %s",
                     (status, Jsonb(result) if result is not None else None, case_id))
        conn.commit()


@traced("approval_gate")
def approval_gate(state: CaseState) -> dict:
    # The approvals row is created with the case (POST /drafts/{id}/submit), in one
    # transaction, so it exists before this node runs. interrupt() persists the pause.
    decision = interrupt({"approval_id": state["approval_id"], "case_id": state["case_id"],
                          "action": state["action"]})
    return {"decision": decision, "_trace": {"detail": {"decision": decision.get("decision")}}}


def route_decision(state: CaseState) -> str:
    return "execute_action" if state["decision"]["decision"] == "approve" else "notify"


@traced("execute_action")
def execute_action(state: CaseState) -> dict:
    """Runs only after approval. Every write here is idempotent by case_id (D18)."""
    a, cid = state["action"], state["case_id"]
    with connect() as conn:
        mismatch = ledger.check_binding(conn, state["approval_id"])
    if mismatch:
        # What would be executed is not what the supervisor approved: post nothing (D55).
        _set_case(cid, "failed", {"error": f"binding check failed: {mismatch}"})
        return {"status": "failed", "_trace": {"status": "blocked", "detail": {"binding": mismatch}}}
    if a["action"] in ("credit", "fee_reversal"):
        try:
            receipt = ledger.post_credit_and_advance(
                case_id=cid, approval_id=state["approval_id"], account_id=a["account_id"], txn_id=a.get("txn_id"),
                amount_inr=a["amount_inr"], kind=a.get("kind", "shadow_credit"),
                reason=a.get("reason", "approved by supervisor"))
            res = ToolResult(ok=True, data={k: v for k, v in receipt.items() if k != "idempotency_key"})
        except ToolError as e:
            res = ToolResult(ok=False, error=str(e))
        # Recovery test hook: the credit transaction is committed, the graph has not checkpointed.
        crash_point("after_credit_commit")
        tool, key = "post_credit_and_advance", "credit"
    elif a["action"] == "chargeback":
        with connect() as conn:
            ev = conn.execute("SELECT evidence FROM cases WHERE case_id = %s", (cid,)).fetchone()[0]
        ids = [e["chunk_id"] for e in ev if e.get("chunk_id")] + [e["txn"]["txn_id"] for e in ev if e.get("txn")]
        res = initiate_chargeback(case_id=cid, txn_id=a["txn_id"], reason_code=a["kind"],
                                  amount_inr=a["amount_inr"], evidence_ids=ids)
        tool, key = "initiate_chargeback", "chargeback"
    else:
        res = None
    if res is None or not res.ok:
        err = res.error if res else f"cannot execute {a['action']!r}"
        _set_case(cid, "failed", {"error": err})
        return {"status": "failed", "_trace": {"status": "error", "detail": {"error": err}}}
    if key != "credit":   # credits advance the case inside their own transaction
        _set_case(cid, "approved", {key: res.data})
    return {"credit": res.data, "status": "approved",
            "_trace": {"detail": {"tool": tool, "result": res.data, "attempts": res.attempts}}}


@traced("notify")
def notify(state: CaseState) -> dict:
    a, d = state["action"], state["decision"]
    if state.get("status") == "failed":
        return {}
    ticket = state["case_id"]
    if d["decision"] == "approve":
        fields = {"amount": f"{a['amount_inr']:,}", "last4": a.get("card_last4") or "XXXX",
                  "ticket_id": ticket,
                  # value date = when the credit was actually posted, not the RBI due date
                  "value_date": (state.get("credit") or {}).get("posted_at", date.today().isoformat())[:10]}
        tpl = {"fee_reversal": "fee_reversed", "chargeback": "chargeback_raised"}.get(a["action"], "credit_approved")
        if tpl == "chargeback_raised":
            fields.pop("value_date")
        final = "closed"
    else:
        fields = {"ticket_id": ticket, "reason": d.get("reason") or "not approved after review"}
        tpl, final = "credit_rejected", "rejected"
    res = draft_customer_notification(case_id=ticket, template=tpl, fields=fields)
    result = {"credit": state.get("credit"), "notification": res.data if res.ok else {"error": res.error},
              "decision": d}
    _set_case(ticket, final, result)
    return {"notification": res.data, "status": final, "_trace": {"detail": {"template": tpl}}}


def build_case_graph():
    g = StateGraph(CaseState)
    g.add_node("approval_gate", approval_gate)
    g.add_node("execute_action", execute_action)
    g.add_node("notify", notify)
    g.add_edge(START, "approval_gate")
    g.add_conditional_edges("approval_gate", route_decision, ["execute_action", "notify"])
    g.add_edge("execute_action", "notify")
    g.add_edge("notify", END)
    return g.compile(checkpointer=get_checkpointer())


_graph = None


def case_graph():
    global _graph
    if _graph is None:
        _graph = build_case_graph()
    return _graph


def _cfg(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


def start_case(case_id: str, approval_id: str, action: dict) -> dict:
    """Run the case graph up to its interrupt. Safe to call twice: a thread that already
    has a checkpoint is left alone."""
    g = case_graph()
    if g.get_state(_cfg(case_id)).values:
        return {"started": False}
    with root_span(case_id, "case: submitted, waiting for approval", approval_id=approval_id):
        g.invoke({"run_id": case_id, "thread_id": case_id, "case_id": case_id,
                  "approval_id": approval_id, "action": action, "status": "pending_approval"}, _cfg(case_id))
    return {"started": True}


def resume_case(case_id: str, decision: dict) -> dict:
    g = case_graph()
    snap = g.get_state(_cfg(case_id))
    if not snap.next:  # already finished (e.g. reconcile beat us to it)
        return dict(snap.values)
    # Paused at the approval interrupt: resume with the decision. Otherwise a node after the
    # gate crashed mid-run (e.g. after the credit committed): continue from the checkpoint,
    # and the node re-runs (its writes are idempotent).
    waiting = any(t.interrupts for t in snap.tasks)
    with root_span(case_id, f"case: resumed on {decision['decision']}", approver_id=decision.get("approver_id")):
        g.invoke(Command(resume=decision) if waiting else None, _cfg(case_id))
    return dict(g.get_state(_cfg(case_id)).values)


def handle_decision_event(payload: dict) -> dict:
    """Outbox handler for ApprovalGranted / ApprovalRejected: resume the case graph.

    A session-level advisory lock per case keeps two dispatchers (the API's inline attempt,
    the worker, a broker consumer, reconcile) from running the same case at once; the loser
    gets Busy and the event is retried. Idempotency still covers a lock lost to a crash."""
    case_id = payload["case_id"]
    with connect() as conn:
        conn.commit()            # connect() ran CREATE EXTENSION; leave that transaction
        conn.autocommit = True   # the lock must not be tied to a transaction we keep open
        if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (case_id,)).fetchone()[0]:
            raise outbox.Busy(case_id)
        try:
            if not case_graph().get_state(_cfg(case_id)).values:
                start_case(case_id, payload["approval_id"], payload["action"])   # never reached its pause
            return resume_case(case_id, {"decision": payload["decision"], "reason": payload.get("reason"),
                                         "approver_id": payload.get("approver_id")})
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (case_id,))


def is_waiting(case_id: str) -> bool:
    return bool(case_graph().get_state(_cfg(case_id)).next)


def reconcile() -> dict[str, int]:
    """Run at startup (and safe at any time). Starts pending cases that never reached their
    checkpoint; makes sure every recorded-but-unexecuted decision has an outbox event (a
    decision from before D55 has none) and dispatches what is due."""
    started = resumed = 0
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.case_id, a.approval_id, a.state, a.reason, a.approver_id, a.action_payload
               FROM approvals a JOIN cases c USING (case_id)
               WHERE c.status IN ('pending_approval', 'at_risk', 'breached')""").fetchall()
    for case_id, approval_id, astate, reason, approver, payload in rows:
        snap = case_graph().get_state(_cfg(case_id))
        if not snap.values:
            start_case(case_id, approval_id, payload); started += 1
        if astate != "pending":
            with connect() as conn:
                b = ledger.binding_for(conn, approval_id)
                # Legacy decision with no binding: bind it to its own immutable payload.
                conn.execute("""UPDATE approvals SET bound_txn_id = %s, bound_amount_inr = %s, bound_draft_version = %s,
                                bound_action_hash = %s WHERE approval_id = %s AND bound_action_hash IS NULL""",
                             (b["txn_id"], b["amount_inr"], b["draft_version"], b["action_hash"], approval_id))
                decision_event(conn, case_id, approval_id, astate, reason, approver, payload)
                conn.commit()
            resumed += 1
    outbox.dispatch_due()
    if started or resumed:
        log.info("reconcile: started=%d resumed=%d", started, resumed)
    return {"started": started, "resumed": resumed}


def decision_event(conn, case_id: str, approval_id: str, state: str, reason: str | None, approver_id: str | None,
                   action: dict) -> str:
    """The outbox event for a decision, on the caller's transaction. Deterministic id."""
    kind = "granted" if state == "approved" else "rejected"
    eid = outbox.event_id_for(approval_id, kind)
    outbox.enqueue(conn, "ApprovalGranted" if kind == "granted" else "ApprovalRejected", case_id,
                   {"case_id": case_id, "approval_id": approval_id, "decision": "approve" if kind == "granted" else "reject",
                    "reason": reason, "approver_id": approver_id, "action": action,
                    "txn_id": action.get("txn_id"), "amount_inr": action.get("amount_inr")}, event_id=eid)
    return eid
