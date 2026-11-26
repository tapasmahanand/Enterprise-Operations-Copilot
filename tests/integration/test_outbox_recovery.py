"""Transactional approval and outbox (D55): crash recovery with real process deaths.

Each step runs in its own Python process. A "crash" is a SimulatedCrash raised at a named
point (KESTREL_CRASH_AT), which ends that process with a traceback right after the commit
it follows, so the next process sees only what Postgres holds.
"""

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


def py(db_url: str, code: str, crash_at: str = "", inline: bool = True, expect_crash: bool = False) -> dict | None:
    env = {**os.environ, "DATABASE_URL": db_url, "KESTREL_CRASH_AT": crash_at,
           "OUTBOX_INLINE_DISPATCH": "1" if inline else "0"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    if expect_crash:
        assert out.returncode != 0 and f"SimulatedCrash: {crash_at}" in out.stderr, out.stderr[-1500:]
        return None
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture()
def db(db_url):
    with connect(db_url) as conn:
        apply_schema(conn)
        seed(conn, anchor=date(2026, 11, 25), force=True)
        conn.execute("TRUNCATE notifications, mock_credits, mock_chargebacks, approvals, cases, drafts, outbox,"
                     " ledger_receipts CASCADE")
        conn.commit()
    return db_url


def submitted_case(db_url: str, action=ACTION) -> dict:
    draft = py(db_url, f"import json; from kestrel.cases import create_draft; "
                       f"print(json.dumps(create_draft('S1', 'R1', {action!r}, [], {CALC!r})))")
    return py(db_url, f"import json; from kestrel.cases import submit_draft; "
                      f"print(json.dumps(submit_draft({draft!r}), default=str))")


def decide_code(apr: str, decision: str = "approve", expected: dict | None = None) -> str:
    return (f"import json; from kestrel.cases import decide; "
            f"print(json.dumps(decide({apr!r}, {decision!r}, 'P01', 'ok', expected={expected!r}), default=str))")


DISPATCH = "import json; from kestrel import outbox; print(json.dumps(outbox.dispatch_due()))"


def counts(db_url: str, case_id: str) -> dict:
    with connect(db_url) as conn:
        q = lambda sql: conn.execute(sql, (case_id,)).fetchone()[0]   # noqa: E731
        return {"credits": q("SELECT count(*) FROM mock_credits WHERE case_id = %s"),
                "receipts": q("SELECT count(*) FROM ledger_receipts WHERE case_id = %s"),
                "notifications": q("SELECT count(*) FROM notifications WHERE case_id = %s"),
                "status": q("SELECT status FROM cases WHERE case_id = %s"),
                "unpublished": q("SELECT count(*) FROM outbox WHERE aggregate_id = %s AND published_at IS NULL")}


def test_decision_binding_and_event_commit_together(db):
    case = submitted_case(db)
    apr = case["approval"]["approval_id"]
    py(db, decide_code(apr), inline=False)
    with connect(db) as conn:
        a = conn.execute("""SELECT state, bound_txn_id, bound_amount_inr, bound_draft_version, bound_action_hash
                            FROM approvals WHERE approval_id = %s""", (apr,)).fetchone()
        ev = conn.execute("SELECT event_id, event_type, payload, published_at FROM outbox WHERE aggregate_id = %s",
                          (case["case_id"],)).fetchall()
    assert a[0] == "approved" and a[1:4] == ("T-HERO", 18450, 1) and len(a[4]) == 64
    assert [(e[0], e[1]) for e in ev] == [(f"EVT-{apr}-granted", "ApprovalGranted")]
    assert ev[0][2]["amount_inr"] == 18450 and ev[0][3] is None      # committed, not yet handled
    assert counts(db, case["case_id"])["credits"] == 0


def test_api_killed_after_approval_commit_completes_after_recovery(db):
    case = submitted_case(db)
    cid = case["case_id"]
    py(db, decide_code(case["approval"]["approval_id"]), crash_at="after_approval_commit", expect_crash=True)
    assert counts(db, cid) == {"credits": 0, "receipts": 0, "notifications": 0, "status": "pending_approval",
                               "unpublished": 1}
    # Recovery: the worker (or reconcile at boot) dispatches the committed event.
    assert py(db, DISPATCH)["dispatched"] == 1
    c = counts(db, cid)
    assert (c["credits"], c["receipts"], c["notifications"], c["status"]) == (1, 1, 1, "closed")
    # The credit transaction added CreditPosted; the next pass hands it on (a no-op until step 3).
    assert c["unpublished"] == 1
    py(db, DISPATCH)
    assert counts(db, cid)["unpublished"] == 0


def test_worker_killed_after_credit_write_replays_with_one_credit(db):
    case = submitted_case(db)
    cid = case["case_id"]
    py(db, decide_code(case["approval"]["approval_id"]), inline=False)
    py(db, DISPATCH, crash_at="after_credit_commit", expect_crash=True)
    c = counts(db, cid)
    assert (c["credits"], c["receipts"], c["notifications"]) == (1, 1, 0)      # credit committed, never acked
    with connect(db) as conn:
        first_credit = conn.execute("SELECT credit_id FROM mock_credits WHERE case_id = %s", (cid,)).fetchone()[0]
        assert conn.execute("SELECT published_at FROM outbox WHERE event_id LIKE '%%-granted' AND aggregate_id = %s",
                            (cid,)).fetchone()[0] is None
        conn.execute("UPDATE outbox SET claimed_until = NULL WHERE aggregate_id = %s", (cid,))   # lease expires
        conn.commit()
    py(db, DISPATCH)                                                            # replay
    c = counts(db, cid)
    assert (c["credits"], c["receipts"], c["notifications"], c["status"]) == (1, 1, 1, "closed")
    with connect(db) as conn:
        assert conn.execute("SELECT credit_id FROM mock_credits WHERE case_id = %s", (cid,)).fetchone()[0] == first_credit
        detail = conn.execute("""SELECT detail FROM run_events WHERE run_id = %s AND node = 'execute_action'
                                 AND status = 'ok' ORDER BY event_id DESC LIMIT 1""", (cid,)).fetchone()[0]
    assert detail["result"]["reused_receipt"] is True


def test_stale_screen_is_refused(db):
    case = submitted_case(db)
    apr = case["approval"]["approval_id"]
    out = py(db, f"""import json; from kestrel.cases import decide, Stale
try:
    decide({apr!r}, 'approve', 'P01', 'ok', expected={{'amount_inr': 1845, 'draft_version': 1}}); print(json.dumps('approved'))
except Stale as s:
    print(json.dumps(s.actual))""", inline=False)
    assert out == {"amount_inr": 18450}
    with connect(db) as conn:
        assert conn.execute("SELECT state FROM approvals WHERE approval_id = %s", (apr,)).fetchone()[0] == "pending"


def test_draft_edit_bumps_version_and_blocks_old_approval(db):
    case = submitted_case(db)
    with connect(db) as conn:
        conn.execute("""UPDATE drafts SET action = jsonb_set(action, '{amount_inr}', '25000')
                        WHERE case_id = %s""", (case["case_id"],))
        conn.commit()
        assert conn.execute("SELECT version FROM drafts WHERE case_id = %s", (case["case_id"],)).fetchone()[0] == 2
    out = py(db, f"""import json; from kestrel.cases import decide, Stale
try:
    decide({case['approval']['approval_id']!r}, 'approve', 'P01', 'ok', expected={{'draft_version': 1}}); print(json.dumps('approved'))
except Stale as s:
    print(json.dumps(s.actual))""", inline=False)
    assert out == {"draft_version": 2}


def test_case_changed_after_approval_posts_nothing(db):
    case = submitted_case(db)
    cid = case["case_id"]
    py(db, decide_code(case["approval"]["approval_id"]), inline=False)
    with connect(db) as conn:   # tamper between decision and execution
        conn.execute("""UPDATE cases SET recommended_action = jsonb_set(recommended_action, '{amount_inr}', '99999')
                        WHERE case_id = %s""", (cid,))
        conn.commit()
    py(db, DISPATCH)
    c = counts(db, cid)
    assert c["credits"] == 0 and c["status"] == "failed"
    with connect(db) as conn:
        assert "binding check failed" in conn.execute("SELECT result->>'error' FROM cases WHERE case_id = %s",
                                                      (cid,)).fetchone()[0]


def test_case_being_executed_elsewhere_is_retried_not_doubled(db):
    """Per-case advisory lock: while another executor holds the case, the event waits."""
    case = submitted_case(db)
    cid = case["case_id"]
    py(db, decide_code(case["approval"]["approval_id"]), inline=False)
    with connect(db) as holder:
        holder.commit(); holder.autocommit = True
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (cid,))
        assert py(db, DISPATCH) == {"dispatched": 0, "failed": 1}
        assert counts(db, cid)["credits"] == 0
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (cid,))
    with connect(db) as conn:
        conn.execute("UPDATE outbox SET claimed_until = NULL"); conn.commit()
    py(db, DISPATCH)
    assert counts(db, cid)["credits"] == 1


def test_rejection_goes_through_the_outbox_too(db):
    case = submitted_case(db)
    py(db, decide_code(case["approval"]["approval_id"], "reject"), crash_at="after_approval_commit", expect_crash=True)
    py(db, DISPATCH)
    c = counts(db, case["case_id"])
    assert (c["credits"], c["notifications"], c["status"]) == (0, 1, "rejected")


def test_remote_ledger_checks_before_retrying_after_a_timeout():
    """Real-API pattern: the first post is applied but its response is lost."""
    from kestrel.ledger import AmbiguousLedgerResult, post_remote_credit

    class Bank:
        def __init__(self, lose_response: bool, apply: bool = True):
            self.applied: dict[str, dict] = {}
            self.posts, self.lose, self.apply = 0, lose_response, apply

        def post(self, *, idempotency_key, account_id, amount_inr, reference):
            self.posts += 1
            if self.apply:
                self.applied.setdefault(idempotency_key, {"credit_id": f"BANK-{len(self.applied) + 1}"})
            if self.lose:
                raise TimeoutError("read timed out")
            return self.applied[idempotency_key]

        def find(self, key):
            return self.applied.get(key)

    bank = Bank(lose_response=True)
    out = post_remote_credit(bank, idempotency_key="credit:APR-1", account_id="A1", amount_inr=100, reference="c")
    assert out == {"credit_id": "BANK-1"} and bank.posts == 1 and len(bank.applied) == 1
    with pytest.raises(AmbiguousLedgerResult):
        post_remote_credit(Bank(lose_response=True, apply=False), idempotency_key="k", account_id="A1",
                           amount_inr=1, reference="c")
