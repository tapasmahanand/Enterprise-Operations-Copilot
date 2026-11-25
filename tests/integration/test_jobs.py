"""Async workflow: Postgres queue, worker, Enron scan, TAT sweeper."""

import os
from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def env(db_url):
    old = dict(os.environ)
    os.environ.update({"DATABASE_URL": db_url})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    from kestrel.db import apply_schema, connect
    from kestrel.ingest.seed import seed
    with connect() as conn:
        apply_schema(conn)
        seed(conn, anchor=date(2026, 11, 24), force=True)
    yield
    os.environ.clear(); os.environ.update(old)


@pytest.fixture(autouse=True)
def clean(env):
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("TRUNCATE jobs, escalations, approvals, cases CASCADE"); conn.commit()


def drain():
    from kestrel.jobs.worker import run_one
    n = 0
    while run_one("test") and n < 20:
        n += 1
    return n


def test_enron_scan_runs_async_and_reports(env):
    from kestrel.jobs import queue
    job = queue.enqueue("enron_compliance_scan", {"limit": 500}, requested_by="compliance")
    assert job["state"] == "queued"            # enqueue returns at once; nothing has run
    drain()
    done = queue.get_job(job["job_id"])
    assert done["state"] == "done" and done["result"]["scanned"] == 500
    assert done["progress"]["processed"] == 500
    for item in done["result"]["sample"]:      # results never carry unmasked PII
        from kestrel.guards.pii import has_unmasked_pii
        assert not has_unmasked_pii(" ".join(f["snippet"] for f in item["flags"]))


def test_dedupe_key_gives_one_live_job(env):
    from kestrel.jobs import queue
    a = queue.enqueue("tat_sweep", {}, dedupe_key="tat_sweep:scheduled")
    b = queue.enqueue("tat_sweep", {}, dedupe_key="tat_sweep:scheduled")
    assert a["job_id"] == b["job_id"]


def test_failing_job_retries_with_backoff_then_fails(env, monkeypatch):
    from kestrel.db import connect
    from kestrel.jobs import queue, worker
    calls = []
    monkeypatch.setitem(worker.HANDLERS, "tat_sweep", lambda p, r: calls.append(1) or (_ for _ in ()).throw(RuntimeError("boom")))
    job = queue.enqueue("tat_sweep", {}, max_attempts=2)
    worker.run_one("t")
    j = queue.get_job(job["job_id"])
    assert j["state"] == "queued" and j["attempts"] == 1 and "boom" in j["error"]
    with connect() as conn:  # skip the backoff wait
        conn.execute("UPDATE jobs SET run_after = now() WHERE job_id = %s", (job["job_id"],)); conn.commit()
    worker.run_one("t")
    assert queue.get_job(job["job_id"])["state"] == "failed" and len(calls) == 2


def test_job_abandoned_by_dead_worker_is_reclaimed_after_lease(env):
    from kestrel.db import connect
    from kestrel.jobs import queue
    job = queue.enqueue("tat_sweep", {})
    assert queue.claim("worker-that-dies")["job_id"] == job["job_id"]
    assert queue.claim("other") is None                       # leased
    with connect() as conn:
        conn.execute("UPDATE jobs SET locked_at = now() - interval '6 minutes' WHERE job_id = %s", (job["job_id"],))
        conn.commit()
    again = queue.claim("other")
    assert again["job_id"] == job["job_id"] and again["attempts"] == 2


def _case(case_id, due, status="pending_approval"):
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("INSERT INTO sessions (session_id, user_id) VALUES ('S', 'a') ON CONFLICT DO NOTHING")
        conn.execute("""INSERT INTO cases (case_id, session_id, status, risk_level, recommended_action, due_at)
                        VALUES (%s, 'S', %s, 'high', %s, %s)""", (case_id, status, Jsonb({}), due))
        conn.commit()


def test_tat_sweep_marks_at_risk_and_breached_and_escalates_once(env):
    from kestrel.db import connect
    from kestrel.jobs import tat_sweep
    today = date(2026, 12, 7)                     # Monday
    _case("C-OK", date(2026, 12, 14))             # 5 working days away
    _case("C-RISK", date(2026, 12, 9))            # 2 working days away
    _case("C-LATE", date(2026, 12, 4))            # passed
    _case("C-DONE", date(2026, 12, 1), status="closed")
    out = tat_sweep.run({"as_of": today.isoformat()}, lambda p: None)
    moved = {c["case_id"]: c["to"] for c in out["changes"]}
    assert moved == {"C-RISK": "at_risk", "C-LATE": "breached"}
    assert len(out["escalations"]) == 1
    again = tat_sweep.run({"as_of": today.isoformat()}, lambda p: None)   # idempotent
    assert again["changes"] == [] and again["escalations"] == out["escalations"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM escalations").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM cases WHERE case_id='C-DONE'").fetchone()[0] == "closed"


def test_stale_worker_cannot_touch_a_reclaimed_job(env):
    """Worker A overruns its lease, B re-claims; A's late progress/complete/fail are no-ops."""
    from kestrel.db import connect
    from kestrel.jobs import queue
    queue.enqueue("tat_sweep", {})
    a = queue.claim("A")
    with connect() as conn:   # A's lease expires
        conn.execute("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE job_id = %s", (a["job_id"],))
        conn.commit()
    b = queue.claim("B")
    assert b["job_id"] == a["job_id"] and b["attempts"] == a["attempts"] + 1
    assert queue.progress(a, {"x": 1}) is False
    assert queue.fail(a, "late error") == "stale"
    assert queue.complete(a, {"by": "A"}) is False
    assert queue.get_job(a["job_id"])["state"] == "running"
    assert queue.complete(b, {"by": "B"}) is True
    done = queue.get_job(b["job_id"])
    assert done["state"] == "done" and done["result"] == {"by": "B"}
    assert queue.fail(a, "even later") == "stale" and queue.get_job(a["job_id"])["state"] == "done"


def test_scan_snippet_masks_labelled_account_numbers():
    from kestrel.jobs.enron_scan import scan_message
    flags = scan_message({"body": "Please wire it to account: 123456789 today.", "subject": "", "recipients": ""})
    acct = [f for f in flags if f["rule_id"] == "CS-PII-ACCT"]
    assert acct and "123456789" not in acct[0]["snippet"] and "XXXX6789" in acct[0]["snippet"]
