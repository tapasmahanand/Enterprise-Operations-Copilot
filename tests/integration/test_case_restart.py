"""The go/no-go property: a case paused at approval survives a process restart.

Each step runs in a *separate Python process*, so nothing survives in memory between
"before restart" and "after restart"; only Postgres does."""

import json
import os
import subprocess
import sys
from datetime import date

import pytest

from kestrel.db import apply_schema, connect
from kestrel.ingest.seed import seed

pytestmark = pytest.mark.db

ACTION = {"action": "credit", "kind": "shadow_credit", "account_id": "A1001", "txn_id": "T-HERO",
          "amount_inr": 18450, "card_last4": "4821", "approver_role": "team_lead", "risk_level": "high"}
CALC = {"shadow_credit_due": "2026-12-16", "rule_ids": ["R17-6ii", "R17-9"]}


def run_py(db_url: str, code: str) -> dict:
    env = {**os.environ, "DATABASE_URL": db_url}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture()
def db(db_url):
    with connect(db_url) as conn:
        apply_schema(conn)
        seed(conn, anchor=date(2026, 11, 25), force=True)
        conn.execute("TRUNCATE notifications, mock_credits, mock_chargebacks, approvals, cases, drafts CASCADE")
        conn.commit()
    return db_url


def new_draft(db_url: str) -> str:
    return run_py(db_url, f"""
import json; from kestrel.cases import create_draft
print(json.dumps(create_draft('S1', 'R1', {ACTION!r}, [], {CALC!r})))""")


def test_approve_after_restart_resumes_and_posts_once(db):
    draft = new_draft(db)
    # Process 1: agent submits; graph runs to interrupt(); process exits ("server restarts").
    case = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    assert case["status"] == "pending_approval"
    # Process 2: nothing in memory. Graph state must come from the checkpoint.
    waiting = run_py(db, f"import json; from kestrel.graphs.case import is_waiting; print(json.dumps(is_waiting({case['case_id']!r})))")
    assert waiting is True
    # Process 3: supervisor approves.
    done = run_py(db, f"""import json; from kestrel.cases import decide
print(json.dumps(decide({case['approval']['approval_id']!r}, 'approve', 'P01', 'verified'), default=str))""")
    assert done["status"] == "closed"
    assert done["result"]["credit"]["amount_inr"] == 18450
    assert "₹18,450" in done["result"]["notification"]["body"]
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM mock_credits WHERE case_id = %s", (case["case_id"],)).fetchone()[0] == 1


def test_second_decision_is_409_and_has_no_side_effect(db):
    draft = new_draft(db)
    case = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    apr = case["approval"]["approval_id"]
    run_py(db, f"import json; from kestrel.cases import decide; print(json.dumps(decide({apr!r}, 'reject', 'P01', 'no evidence'), default=str))")
    second = run_py(db, f"""import json; from kestrel.cases import decide, Conflict
try:
    decide({apr!r}, 'approve', 'P02', 'overrule'); print(json.dumps('no-conflict'))
except Conflict as c:
    print(json.dumps(c.existing, default=str))""")
    assert second["state"] == "rejected" and second["approver_id"] == "P01"
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM mock_credits").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM cases WHERE case_id = %s", (case["case_id"],)).fetchone()[0] == "rejected"


def test_double_submit_returns_same_case(db):
    draft = new_draft(db)
    a = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    b = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    assert a["case_id"] == b["case_id"]


def test_crash_between_decision_and_resume_is_reconciled_on_boot(db):
    draft = new_draft(db)
    case = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    # Simulate: decision committed, process died before Command(resume=...) ran.
    with connect(db) as conn:
        conn.execute("UPDATE approvals SET state='approved', approver_id='P01', decided_at=now() WHERE case_id=%s",
                     (case["case_id"],))
        conn.commit()
    out = run_py(db, "import json; from kestrel.graphs.case import reconcile; print(json.dumps(reconcile()))")
    assert out == {"started": 0, "resumed": 1}
    with connect(db) as conn:
        status, = conn.execute("SELECT status FROM cases WHERE case_id=%s", (case["case_id"],)).fetchone()
        credits, = conn.execute("SELECT count(*) FROM mock_credits WHERE case_id=%s", (case["case_id"],)).fetchone()
    assert status == "closed" and credits == 1


def test_crash_before_graph_start_is_reconciled_on_boot(db):
    draft = new_draft(db)
    # Simulate: case + approval committed, process died before start_case().
    run_py(db, f"""import json, kestrel.cases as c
c.start_case = lambda *a, **k: None
print(json.dumps(c.submit_draft({draft!r}), default=str))""")
    out = run_py(db, "import json; from kestrel.graphs.case import reconcile; print(json.dumps(reconcile()))")
    assert out == {"started": 1, "resumed": 0}


@pytest.mark.parametrize("action, tpl_text, table", [
    ({"action": "fee_reversal", "kind": "fee_reversal", "account_id": "A1001", "txn_id": "T1003", "amount_inr": 23,
      "card_last4": "4821", "approver_role": "team_lead"}, "fee on your Kestrel account", "mock_credits"),
    ({"action": "chargeback", "kind": "not_received", "account_id": "A2001", "txn_id": "T2001", "amount_inr": 4999,
      "card_last4": "7710", "approver_role": "team_lead"}, "raised a dispute with the merchant", "mock_chargebacks"),
])
def test_other_actions_execute_after_restart(db, action, tpl_text, table):
    evidence = [{"chunk_id": "KB-SOP-CHARGEBACK:0"}, {"tool": "transaction_lookup", "txn": {"txn_id": action["txn_id"]}}]
    draft = run_py(db, f"""
import json; from kestrel.cases import create_draft
print(json.dumps(create_draft('S1', 'R1', {action!r}, {evidence!r}, None)))""")
    case = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    done = run_py(db, f"""import json; from kestrel.cases import decide
print(json.dumps(decide({case['approval']['approval_id']!r}, 'approve', 'P01', 'ok'), default=str))""")
    assert done["status"] == "closed" and tpl_text in done["result"]["notification"]["body"]
    with connect(db) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE case_id = %s", (case["case_id"],)).fetchone()[0] == 1


def test_team_lead_cannot_approve_above_tier(db):
    big = {**ACTION, "amount_inr": 64000, "approver_role": "ops_manager"}
    draft = run_py(db, f"import json; from kestrel.cases import create_draft; print(json.dumps(create_draft('S1','R1',{big!r},[],None)))")
    case = run_py(db, f"import json; from kestrel.cases import submit_draft; print(json.dumps(submit_draft({draft!r}), default=str))")
    out = run_py(db, f"""import json; from kestrel.cases import decide, Forbidden
try:
    decide({case['approval']['approval_id']!r}, 'approve', 'P01', 'ok'); print(json.dumps('allowed'))
except Forbidden as f:
    print(json.dumps('forbidden'))""")
    assert out == "forbidden"
