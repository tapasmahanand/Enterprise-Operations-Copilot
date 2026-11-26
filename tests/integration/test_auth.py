"""Login, sessions, identity from the session only, and the append-only audit log (D50-D52).
Goes through the HTTP API with the fake LLM and embedder (no key, no network)."""

import json
import os
import uuid
from datetime import date

import psycopg
import pytest

pytestmark = pytest.mark.db
ANCHOR = date(2026, 11, 24)
PW = "kestrel-demo"
HERO = "Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, it went through yesterday"


@pytest.fixture(scope="module")
def app_client(db_url):
    old = dict(os.environ)
    os.environ.update({"DATABASE_URL": db_url, "LLM_PROVIDER": "fake", "EMBEDDING_PROVIDER": "fake",
                       "SEED_ANCHOR_DATE": ANCHOR.isoformat(), "RETRIEVAL_MIN_SCORE": "0"})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("KESTREL_DEMO_PASSWORD", None)
    from fastapi.testclient import TestClient

    from kestrel.config import get_settings
    from kestrel.db import apply_schema, connect
    from kestrel.ingest.embed import FakeEmbedder
    from kestrel.ingest.run import ingest_corpus
    from kestrel.ingest.seed import seed
    from kestrel.llm import get_llm
    get_llm.cache_clear()
    from conftest import reset_graph_singletons
    reset_graph_singletons()
    with connect() as conn:
        apply_schema(conn)
        seed(conn, anchor=ANCHOR, force=True)
        ingest_corpus(conn, get_settings(), FakeEmbedder())
    from kestrel.api.main import app
    with TestClient(app) as c:
        yield c
    os.environ.clear(); os.environ.update(old)


@pytest.fixture()
def client(app_client):
    app_client.cookies.clear()      # every test starts signed out
    return app_client


def bearer(client, user, password=PW):
    r = client.post("/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()          # use the header, so tests can act as several users at once
    return {"Authorization": f"Bearer {r.json()['token']}"}


def chat(client, headers, message=HERO, idv=2, session=None):
    r = client.post("/chat", json={"message": message, "idv_level": idv, **({"session_id": session} if session else {})},
                    headers=headers)
    assert r.status_code == 200, r.text
    events = []
    with client.stream("GET", r.json()["stream"], headers=headers) as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return r.json(), next(e for e in events if e["type"] == "final")


def reset_bank():
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("UPDATE mock_cards SET status = 'active'")
        conn.execute("TRUNCATE notifications, mock_credits, mock_chargebacks CASCADE")
        conn.commit()


def audit_rows(action=None, object_id=None):
    from kestrel.db import connect
    with connect() as conn:
        return conn.execute("""SELECT actor_user_id, actor_role, actor_person_id, action, object_id, detail
                               FROM audit_log WHERE (%s::text IS NULL OR action = %s) AND (%s::text IS NULL OR object_id = %s)
                               ORDER BY audit_id""", (action, action, object_id, object_id)).fetchall()


# --- no session, no access --------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("post", "/chat", {"message": "hello there"}),
    ("get", "/approvals", None),
    ("post", "/approvals/APR-X", {"decision": "approve"}),
    ("post", "/drafts/DRF-X/submit", None),
    ("post", "/jobs", {"job_type": "tat_sweep"}),
    ("get", "/audit", None),
])
def test_everything_but_health_and_login_needs_a_session(client, method, path, body):
    r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 401


def test_x_role_header_grants_nothing(client):
    """The old trust model: any client could claim a role in a header."""
    assert client.get("/approvals", headers={"X-Role": "supervisor"}).status_code == 401
    agent = bearer(client, "agent1")
    assert client.get("/approvals", headers={**agent, "X-Role": "supervisor"}).status_code == 403


def test_health_and_ui_stay_public(client):
    assert client.get("/health").status_code in (200, 503)
    assert client.get("/").status_code == 200


# --- login ----------------------------------------------------------------------------------

def test_wrong_password_is_refused_and_audited(client):
    r = client.post("/auth/login", json={"username": "farah", "password": "nope"})
    assert r.status_code == 401
    assert ("farah", ) == audit_rows("login_failed", "farah")[-1][4:5]


def test_repeated_failures_are_throttled(client):
    for _ in range(5):
        client.post("/auth/login", json={"username": "agent2", "password": "wrong"})
    r = client.post("/auth/login", json={"username": "agent2", "password": PW})
    assert r.status_code == 429      # even the right password, until the window passes
    from kestrel import auth
    auth._FAILS.clear()


def test_cookie_session_and_logout_revokes_it(client):
    r = client.post("/auth/login", json={"username": "ananya", "password": PW})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    token = r.json()["token"]
    me = client.get("/auth/me").json()
    assert me["user_id"] == "ananya" and me["person_id"] == "P01" and me["approver_role"] == "team_lead"
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_only_a_hash_of_the_token_is_stored(client):
    h = bearer(client, "agent1")
    token = h["Authorization"].split()[1]
    from kestrel.db import connect
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM auth_sessions WHERE token_hash = %s", (token,)).fetchone()[0] == 0


def test_expired_session_is_refused(client):
    h = bearer(client, "agent1")
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 second' WHERE user_id = 'agent1'")
        conn.commit()
    assert client.get("/auth/me", headers=h).status_code == 401


# --- identity comes from the session --------------------------------------------------------

def test_full_flow_records_who_did_what(client):
    reset_bank()
    a1, sup = bearer(client, "agent1"), bearer(client, "ananya")
    run, final = chat(client, a1)
    draft = final["draft"]
    assert draft and draft["amount_inr"] == 18450 and draft["approver_role"] == "team_lead"

    case = client.post(f"/drafts/{draft['draft_id']}/submit", headers=a1).json()
    assert case["submitted_by"] == "agent1"
    apr = case["approval"]["approval_id"]

    # The approver id can no longer be sent: the body is rejected, not silently trusted.
    spoof = client.post(f"/approvals/{apr}", headers=sup, json={"decision": "approve", "reason": "x", "approver_id": "P04"})
    assert spoof.status_code == 422

    done = client.post(f"/approvals/{apr}", headers=sup, json={"decision": "approve", "reason": "evidence reviewed"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "closed" and body["approval"]["approver_id"] == "P01"
    assert body["approval"]["decided_by"] == "ananya"

    rows = audit_rows(object_id=apr)
    decided = [r for r in rows if r[3] == "approval_decided"][-1]
    assert decided[:3] == ("ananya", "supervisor", "P01")
    assert decided[5]["approver_tier"] == "team_lead" and decided[5]["decision"] == "approve"
    chat_row = audit_rows("chat", run["run_id"])[-1]
    assert chat_row[0] == "agent1" and chat_row[5]["idv_level_attested"] == 2
    submitted = audit_rows("draft_submitted", case["case_id"])[-1]
    assert submitted[0] == "agent1" and submitted[5]["amount_inr"] == 18450


def test_agent_cannot_submit_or_read_another_agents_work(client):
    reset_bank()
    a1, a2 = bearer(client, "agent1"), bearer(client, "agent2")
    run, final = chat(client, a1)
    r = client.post(f"/drafts/{final['draft']['draft_id']}/submit", headers=a2)
    assert r.status_code == 403
    assert audit_rows("draft_submit_denied", final["draft"]["draft_id"])[-1][0] == "agent2"
    assert client.get(f"/runs/{run['run_id']}", headers=a2).status_code == 404
    # ...nor continue their call session (which carries the caller's details in memory).
    r = client.post("/chat", json={"message": "and the other card?", "session_id": run["session_id"]}, headers=a2)
    assert r.status_code == 403
    case = client.post(f"/drafts/{final['draft']['draft_id']}/submit", headers=a1).json()
    assert client.get(f"/cases/{case['case_id']}", headers=a2).status_code == 404
    assert client.get(f"/cases/{case['case_id']}", headers=a1).status_code == 200


def test_another_user_cannot_stream_my_run(client):
    a1, a2 = bearer(client, "agent1"), bearer(client, "agent2")
    r = client.post("/chat", json={"message": HERO, "idv_level": 2}, headers=a1).json()
    assert client.get(r["stream"], headers=a2).status_code == 404
    with client.stream("GET", r["stream"], headers=a1) as s:   # still there for its owner
        assert s.status_code == 200
        list(s.iter_lines())


def test_agent_cannot_approve_and_team_lead_cannot_approve_above_tier(client):
    """An ops-manager case: the team lead is refused, the ops manager approves."""
    from kestrel.cases import create_draft
    from kestrel.db import connect
    reset_bank()
    a1 = bearer(client, "agent1")
    run_id, sess = f"RUN-T{uuid.uuid4().hex[:6]}", f"S-T{uuid.uuid4().hex[:6]}"
    with connect() as conn:
        conn.execute("INSERT INTO chat_runs (run_id, session_id, message, user_id) VALUES (%s, %s, 'x', 'agent1')",
                     (run_id, sess))
        conn.commit()
    action = {"action": "credit", "kind": "refund", "account_id": "A2001", "txn_id": "T2002", "amount_inr": 4999,
              "approver_role": "ops_manager", "risk_level": "high"}
    draft_id = create_draft(sess, run_id, action, [], None)
    apr = client.post(f"/drafts/{draft_id}/submit", headers=a1).json()["approval"]["approval_id"]

    assert client.post(f"/approvals/{apr}", headers=a1, json={"decision": "approve", "reason": "x"}).status_code == 403
    lead = client.post(f"/approvals/{apr}", headers=bearer(client, "ananya"), json={"decision": "approve", "reason": "x"})
    assert lead.status_code == 403 and "ops_manager" in lead.json()["detail"]
    denied = audit_rows("approval_denied", apr)[-1]
    assert denied[0] == "ananya" and denied[5]["needs"] == "ops_manager"
    ok = client.post(f"/approvals/{apr}", headers=bearer(client, "farah"), json={"decision": "approve", "reason": "ok"})
    assert ok.status_code == 200 and ok.json()["approval"]["approver_id"] == "P03"


def test_separation_of_duties_in_the_domain_layer(client):
    """Roles already stop a supervisor submitting through the API; the case layer also refuses
    a decision by the user who submitted the case, whatever their tier."""
    from kestrel import cases
    from kestrel.auth import User
    from kestrel.db import connect
    run_id, sess = f"RUN-S{uuid.uuid4().hex[:6]}", f"S-S{uuid.uuid4().hex[:6]}"
    rohan = User("rohan", "Rohan Iyer", "supervisor", "P04", "regional_ops_head")
    with connect() as conn:
        conn.execute("INSERT INTO chat_runs (run_id, session_id, message, user_id) VALUES (%s, %s, 'x', 'rohan')",
                     (run_id, sess))
        conn.commit()
    action = {"action": "credit", "kind": "refund", "account_id": "A2001", "txn_id": "T2002", "amount_inr": 4999,
              "approver_role": "team_lead", "risk_level": "high"}
    case = cases.submit_draft(cases.create_draft(sess, run_id, action, [], None), submitted_by=rohan)
    with pytest.raises(cases.Forbidden, match="submitted this case"):
        cases.decide(case["approval"]["approval_id"], "approve", None, "self", decided_by=rohan)


def test_only_compliance_starts_a_scan_and_jobs_record_the_user(client):
    assert client.post("/jobs", headers=bearer(client, "ananya"),
                       json={"job_type": "enron_compliance_scan"}).status_code == 403
    r = client.post("/jobs", headers=bearer(client, "compliance1"), json={"job_type": "enron_compliance_scan",
                                                                           "payload": {"limit": 5}})
    assert r.status_code == 202 and r.json()["requested_by"] == "compliance1"
    assert audit_rows("job_created", r.json()["job_id"])[-1][0] == "compliance1"


def test_audit_endpoint_is_for_supervisors_and_compliance(client):
    assert client.get("/audit", headers=bearer(client, "agent1")).status_code == 403
    rows = client.get("/audit?limit=5", headers=bearer(client, "compliance1")).json()
    assert 0 < len(rows) <= 5 and rows[0]["audit_id"] > rows[-1]["audit_id"]


# --- the audit log is append-only -----------------------------------------------------------

@pytest.mark.parametrize("sql", ["UPDATE audit_log SET actor_user_id = 'rohan'", "DELETE FROM audit_log",
                                 "TRUNCATE audit_log"])
def test_audit_log_rejects_changes(client, sql):
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("INSERT INTO audit_log (action, object_type) VALUES ('test', 'test')")
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql)
        conn.rollback()
