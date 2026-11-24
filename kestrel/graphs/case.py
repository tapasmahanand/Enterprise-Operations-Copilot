"""Case graph: one thread per drafted action (thread_id = case_id).

    approval_gate (interrupt) ──approved──► execute_action ─► notify ─► END
                              └─rejected──► notify ─► END

Restart safety rests on three things:
1. the pause is a Postgres checkpoint, not process memory;
2. every node is safe to re-run (LangGraph re-executes an interrupted node from its
   start on resume, and a crash can land between a side effect and its checkpoint):
   approval rows, credits and notifications are all keyed by case_id;
3. `reconcile()` at startup finishes any case whose decision was recorded but whose
   resume never ran (the app died between the two).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

from kestrel.checkpoint import get_checkpointer
from kestrel.db import connect
from kestrel.tools.bank import draft_customer_notification, submit_credit
from kestrel.tracing import traced

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
    a = state["action"]
    if a["action"] != "credit":
        raise ValueError(f"case graph cannot execute {a['action']!r} yet")  # M3: fee reversal, chargeback
    res = submit_credit(case_id=state["case_id"], account_id=a["account_id"], txn_id=a.get("txn_id"),
                        amount_inr=a["amount_inr"], kind=a.get("kind", "shadow_credit"),
                        reason=a.get("reason", "approved by supervisor"))
    if not res.ok:
        _set_case(state["case_id"], "failed", {"error": res.error})
        return {"status": "failed", "_trace": {"status": "error", "detail": {"error": res.error}}}
    _set_case(state["case_id"], "approved", {"credit": res.data})
    return {"credit": res.data, "status": "approved",
            "_trace": {"detail": {"tool": "submit_credit", "credit_id": res.data["credit_id"], "attempts": res.attempts}}}


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
        tpl, final = "credit_approved", "closed"
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
    g.invoke({"run_id": case_id, "thread_id": case_id, "case_id": case_id,
              "approval_id": approval_id, "action": action, "status": "pending_approval"}, _cfg(case_id))
    return {"started": True}


def resume_case(case_id: str, decision: dict) -> dict:
    g = case_graph()
    snap = g.get_state(_cfg(case_id))
    if not snap.next:  # already finished (e.g. reconcile beat us to it)
        return dict(snap.values)
    g.invoke(Command(resume=decision), _cfg(case_id))
    return dict(g.get_state(_cfg(case_id)).values)


def is_waiting(case_id: str) -> bool:
    return bool(case_graph().get_state(_cfg(case_id)).next)


def reconcile() -> dict[str, int]:
    """Run at startup. Starts pending cases that never reached their checkpoint, and
    resumes cases whose decision was recorded but not applied."""
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
            resume_case(case_id, {"decision": "approve" if astate == "approved" else "reject",
                                  "reason": reason, "approver_id": approver}); resumed += 1
    if started or resumed:
        log.info("reconcile: started=%d resumed=%d", started, resumed)
    return {"started": started, "resumed": resumed}
