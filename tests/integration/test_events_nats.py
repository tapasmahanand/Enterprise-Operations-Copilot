"""Event-driven execution over NATS JetStream (D56), with a real nats-server.

Skipped when no `nats-server` binary is on PATH (or NATS_SERVER_BIN). The events service
runs as its own process, as in compose; the broker is stopped and restarted for real.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import date

import pytest

from kestrel.db import apply_schema, connect
from kestrel.ingest.seed import seed

pytestmark = pytest.mark.db
NATS_BIN = os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")
ACTION = {"action": "credit", "kind": "shadow_credit", "account_id": "A1001", "txn_id": "T-HERO",
          "amount_inr": 18450, "card_last4": "4821", "approver_role": "team_lead", "risk_level": "high"}
# Seeded transactions that are not credited yet, so three cases can each post (D38: one per txn).
TXNS = [("A1001", "T-HERO", 18450), ("A1001", "T1003", 23), ("A2001", "T2001", 4999)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Broker:
    def __init__(self, store: str):
        self.port, self.store, self.proc = free_port(), store, None

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    def start(self):
        self.proc = subprocess.Popen([NATS_BIN, "-js", "-sd", self.store, "-a", "127.0.0.1", "-p", str(self.port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("nats-server did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate(); self.proc.wait(10); self.proc = None


@pytest.fixture()
def broker(tmp_path):
    if not NATS_BIN:
        pytest.skip("nats-server not installed (set NATS_SERVER_BIN)")
    b = Broker(str(tmp_path / "js"))
    b.start()
    yield b
    b.stop()


@pytest.fixture()
def db(db_url):
    with connect(db_url) as conn:
        apply_schema(conn)
        seed(conn, anchor=date(2026, 11, 25), force=True)
        conn.execute("TRUNCATE notifications, mock_credits, mock_chargebacks, approvals, cases, drafts, outbox,"
                     " ledger_receipts CASCADE")
        conn.commit()
    return db_url


def env(db_url, broker, **extra):
    e = {**os.environ, "DATABASE_URL": db_url, "NATS_URL": broker.url, "NATS_STREAM": f"T{broker.port}",
         "RECONCILE_INTERVAL_S": "3600", "METRICS_INTERVAL_S": "3600", "EVENTS_ACK_WAIT_S": "3"}
    e.pop("KESTREL_CRASH_AT", None)
    return {**e, **extra}


def py(db_url, broker, code: str) -> object:
    out = subprocess.run([sys.executable, "-c", code], env=env(db_url, broker), capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


class Service:
    """python -m kestrel.events.run as a separate process."""
    def __init__(self, db_url, broker, **extra):
        self.log = open(f"/tmp/events-{broker.port}-{time.time_ns()}.log", "w+")
        self.proc = subprocess.Popen([sys.executable, "-m", "kestrel.events.run"], env=env(db_url, broker, **extra),
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def text(self) -> str:
        self.log.flush(); self.log.seek(0)
        return self.log.read()

    def wait_up(self):
        for _ in range(200):
            if "events service up" in self.text():
                return
            assert self.proc.poll() is None, self.text()[-2000:]
            time.sleep(0.05)
        raise AssertionError(self.text()[-2000:])

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def new_case(db_url, broker, account, txn, amount) -> dict:
    a = {**ACTION, "account_id": account, "txn_id": txn, "amount_inr": amount,
         "kind": "fee_reversal" if amount == 23 else "shadow_credit",
         "action": "fee_reversal" if amount == 23 else "credit"}
    return py(db_url, broker, f"""import json; from kestrel.cases import create_draft, submit_draft
print(json.dumps(submit_draft(create_draft('S1', 'R1', {a!r}, [], None)), default=str))""")


def approve(db_url, broker, case) -> dict:
    return py(db_url, broker, f"""import json; from kestrel.cases import decide
print(json.dumps(decide({case['approval']['approval_id']!r}, 'approve', 'P01', 'ok'), default=str))""")


def state(db_url):
    with connect(db_url) as conn:
        return {"credits": conn.execute("SELECT count(*) FROM mock_credits").fetchone()[0],
                "receipts": conn.execute("SELECT count(*) FROM ledger_receipts").fetchone()[0],
                "notifications": conn.execute("SELECT count(*) FROM notifications").fetchone()[0],
                "closed": conn.execute("SELECT count(*) FROM cases WHERE status = 'closed'").fetchone()[0],
                "unpublished": conn.execute("SELECT count(*) FROM outbox WHERE published_at IS NULL").fetchone()[0]}


def wait_for(pred, timeout=40.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def republish(broker, db_url):
    """Duplicate deliveries: every event again, with fresh message ids so the broker's
    duplicate filter does not drop them; only consumer idempotency can save us."""
    import nats

    async def go():
        nc = await nats.connect(broker.url)
        js = nc.jetstream()
        with connect(db_url) as conn:
            rows = conn.execute("SELECT event_id, event_type, payload FROM outbox").fetchall()
        subj = {"ApprovalGranted": "kestrel.events.approval.granted", "CreditPosted": "kestrel.events.credit.posted"}
        for eid, et, payload in rows:
            if et in subj:
                await js.publish(subj[et], json.dumps(payload).encode(), headers={"Nats-Msg-Id": f"{eid}-dup"})
        await nc.close()
        return len(rows)
    return asyncio.run(go())


def test_broker_outage_backlog_drains_and_duplicates_post_once(db, broker):
    svc = Service(db, broker)
    try:
        svc.wait_up()
        cases = [new_case(db, broker, *t) for t in TXNS]
        broker.stop()                                                  # broker down...
        decided = [approve(db, broker, c) for c in cases]              # ...approvals continue
        assert all(d["approval"]["state"] == "approved" and d["status"] == "pending_approval" for d in decided)
        time.sleep(2.5)
        s = state(db)
        assert (s["credits"], s["unpublished"]) == (0, 3), s           # waiting in the outbox, not lost
        from kestrel.events.metrics import snapshot
        os.environ["DATABASE_URL"] = db
        lag = snapshot()
        assert lag["outbox_unpublished"] == 3 and lag["oldest_unpublished_age_s"] >= 2
        broker.start()                                                 # broker back
        wait_for(lambda: state(db)["closed"] == 3, what="backlog to drain")
        wait_for(lambda: state(db)["unpublished"] == 0, what="CreditPosted to publish")
        assert state(db) == {"credits": 3, "receipts": 3, "notifications": 3, "closed": 3, "unpublished": 0}
        assert republish(broker, db) >= 6
        time.sleep(4)                                                  # consumers chew through the duplicates
        assert svc.proc.poll() is None, svc.text()[-2000:]
        assert state(db) == {"credits": 3, "receipts": 3, "notifications": 3, "closed": 3, "unpublished": 0}
        m = snapshot()
        assert m["approval_to_post_s"]["n"] == 3 and m["approval_to_post_s"]["max"] >= 2   # includes the outage
        assert m["outbox_unpublished"] == 0 and m["oldest_unpublished_age_s"] is None
    finally:
        svc.stop()


def test_consumer_killed_after_credit_commit_is_redelivered_once(db, broker):
    """Ack happens after durable progress: the executor dies after the credit commits and
    before it acks; JetStream redelivers to the restarted service, which reuses the receipt."""
    case = new_case(db, broker, *TXNS[0])
    svc = Service(db, broker, KESTREL_CRASH_AT="after_credit_commit")
    try:
        svc.wait_up()
        approve(db, broker, case)
        wait_for(lambda: svc.proc.poll() is not None, 20, "executor to crash")
        assert "SimulatedCrash" in svc.text()
        s = state(db)
        assert (s["credits"], s["receipts"], s["notifications"], s["closed"]) == (1, 1, 0, 0)
    finally:
        svc.stop()
    svc2 = Service(db, broker)
    try:
        svc2.wait_up()
        wait_for(lambda: state(db)["closed"] == 1, 30, "redelivery to finish the case")
        assert state(db) == {"credits": 1, "receipts": 1, "notifications": 1, "closed": 1, "unpublished": 0}
    finally:
        svc2.stop()


def test_reconcile_finishes_a_published_but_stalled_case(db, monkeypatch):
    """A decision whose event was published but never consumed is executed directly."""
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.delenv("NATS_URL", raising=False)
    from conftest import reset_graph_singletons
    reset_graph_singletons()
    from kestrel.cases import create_draft, decide, submit_draft
    from kestrel.events.run import reconcile_stalled
    monkeypatch.setenv("OUTBOX_INLINE_DISPATCH", "0")
    case = submit_draft(create_draft("S1", "R1", ACTION, [], None))
    decide(case["approval"]["approval_id"], "approve", "P01", "ok")
    with connect(db) as conn:      # "published" to a broker whose consumer lost it
        conn.execute("UPDATE outbox SET published_at = now() - interval '1 hour'"); conn.commit()
    reconcile_stalled(60)
    with connect(db) as conn:
        conn.execute("UPDATE outbox SET published_at = now() - interval '1 hour'"); conn.commit()
    reconcile_stalled(60)
    s = state(db)
    assert (s["credits"], s["notifications"], s["closed"]) == (1, 1, 1)
    reset_graph_singletons()
