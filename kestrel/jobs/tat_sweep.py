"""TAT deadline sweeper (scheduled async job).

Every pending case carries an RBI-derived due date from the calculator (cases.due_at).
The sweeper marks cases `at_risk` when the due date is within AT_RISK_WORKING_DAYS
working days, and `breached` once it has passed; a breach also drafts a grievance
escalation (no approval needed, KB-MATRIX). Idempotent: re-running changes nothing."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from kestrel.db import connect
from kestrel.rules.calculator import working_days_after
from kestrel.rules.loader import load_rules
from kestrel.tools.bank import escalate_complaint

AT_RISK_WORKING_DAYS = 2
LIVE = ("pending_approval", "at_risk", "breached")


def classify(due: date, today: date) -> str:
    if today > due:
        return "breached"
    if working_days_after(today, due, load_rules().calendar) <= AT_RISK_WORKING_DAYS:
        return "at_risk"
    return "pending_approval"


def run(payload: dict[str, Any], report: Callable[[dict], None]) -> dict[str, Any]:
    today = date.fromisoformat(payload["as_of"]) if payload.get("as_of") else date.today()
    changes, escalated = [], []
    with connect() as conn:
        rows = conn.execute("SELECT case_id, status, due_at FROM cases WHERE status = ANY(%s) AND due_at IS NOT NULL",
                            (list(LIVE),)).fetchall()
    for case_id, status, due in rows:
        new = classify(due, today)
        if new != status:
            with connect() as conn:
                # Guard on the old status so a case decided meanwhile is never overwritten.
                n = conn.execute("UPDATE cases SET status = %s, updated_at = now() WHERE case_id = %s AND status = %s",
                                 (new, case_id, status)).rowcount
                conn.commit()
            if n:
                changes.append({"case_id": case_id, "from": status, "to": new, "due_at": due.isoformat()})
        if new == "breached":
            r = escalate_complaint(case_id=case_id, level="grievance", created_by="tat_sweeper", rule_id="KB-SOP-ESC",
                                   reason=f"RBI-derived due date {due.isoformat()} passed without a decision")
            if r.ok:
                escalated.append(r.data["escalation_id"])
    report({"checked": len(rows)})
    return {"as_of": today.isoformat(), "checked": len(rows), "changes": changes,
            "escalations": sorted(set(escalated))}
