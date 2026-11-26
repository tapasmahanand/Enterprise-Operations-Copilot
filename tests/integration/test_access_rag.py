"""Access-controlled retrieval (D54), proved through the HTTP API with signed-in users.

The setup makes the restricted Fraud Operations playbook the closest match for the question
(checked first, without the filter), then asks the same question as a tier-1 agent and as a
fraud specialist. The tier-1 run must not carry the playbook anywhere: not in any prompt sent
to the model, not in the citation candidates, the citations or the answer, not in the run's
trace rows and not in GET /runs. The fraud specialist's run must get it.
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.db
ANCHOR = date(2026, 11, 24)
PW = "kestrel-demo"
DOC = "KB-FRAUD-PLAYBOOK"
# Words only the restricted playbook uses, so a leak is visible in any text we capture.
TELLTALES = (DOC, "Fraud Operations Investigation Playbook", "FRM-7", "820")
# The fake supervisor routes "what is ... policy" to rag; the fake rewrite keeps the words.
QUESTION = "What is the policy on mule account indicators and a debit freeze on the receiving account?"


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
def prompts(monkeypatch):
    """Every system+user text the graph sends to the (fake) model during the test."""
    from kestrel.llm import FakeLLM
    seen: list[str] = []
    for name in ("structured", "text", "stream"):
        orig = getattr(FakeLLM, name)

        def spy(self, system, user, *a, _orig=orig, **k):
            seen.append(f"{system}\n{user}")
            return _orig(self, system, user, *a, **k)
        monkeypatch.setattr(FakeLLM, name, spy)
    return seen


def login(client, user):
    r = client.post("/auth/login", json={"username": user, "password": PW})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def ask(client, headers, message=QUESTION):
    r = client.post("/chat", json={"message": message, "idv_level": 2}, headers=headers)
    assert r.status_code == 200, r.text
    events = []
    with client.stream("GET", r.json()["stream"], headers=headers) as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return r.json()["run_id"], events


def leaks(text: str) -> list[str]:
    return [t for t in TELLTALES if t in text]


def test_restricted_doc_is_the_closest_match_without_the_filter(app_client):
    """Precondition: without access control the playbook would be the top hit, so the
    tier-1 assertions below are not passing by luck of ranking."""
    from kestrel.access import Principal
    from kestrel.db import connect
    from kestrel.ingest.embed import FakeEmbedder
    from kestrel.retrieval.search import vector_search
    everyone = Principal("test", frozenset({"all_staff", "fraud_operations", "compliance", "supervisors"}))
    with connect() as conn:
        hits = vector_search(conn, FakeEmbedder().embed([QUESTION])[0], k=3, principal=everyone)
    assert hits[0].document_id == DOC


def test_tier1_agent_never_sees_the_restricted_doc(app_client, prompts):
    h = login(app_client, "agent1")
    run_id, events = ask(app_client, h)
    final = next(e for e in events if e["type"] == "final")
    cands = next(e for e in events if e["type"] == "citations_candidates")["chunks"]
    assert cands, "tier-1 should still get permitted chunks"
    assert not any(c["document_id"] == DOC for c in cands)
    assert not any(c.startswith(DOC) for c in final["citations"])
    assert leaks(json.dumps(events)) == []
    # Model context: the supervisor prompt (KB topic titles), the rewrite and the composer.
    assert prompts and all(leaks(p) == [] for p in prompts), [leaks(p) for p in prompts]
    # Trace rows, and the run as the agent reads it back.
    from kestrel.db import connect
    with connect() as conn:
        rows = conn.execute("SELECT detail FROM run_events WHERE run_id = %s", (run_id,)).fetchall()
    assert rows and leaks(json.dumps([r[0] for r in rows])) == []
    r = app_client.get(f"/runs/{run_id}", headers=h)
    assert r.status_code == 200 and leaks(r.text) == []


def test_fraud_specialist_gets_the_restricted_doc(app_client, prompts):
    h = login(app_client, "fraud1")
    assert "fraud_operations" in app_client.get("/auth/me", headers=h).json()["groups"]
    run_id, events = ask(app_client, h)
    final = next(e for e in events if e["type"] == "final")
    cands = next(e for e in events if e["type"] == "citations_candidates")["chunks"]
    assert cands[0]["document_id"] == DOC
    assert f"{DOC}:0" in final["citations"]
    assert any("FRM-7" in p for p in prompts)       # the composer saw the playbook text


def test_supervisor_reading_the_specialists_run_gets_it_redacted(app_client):
    """Supervisors may read any agent's run, but not documents outside their own groups."""
    run_id, _ = ask(app_client, login(app_client, "fraud1"))
    r = app_client.get(f"/runs/{run_id}", headers=login(app_client, "ananya"))
    assert r.status_code == 200
    assert leaks(r.text) == [], [r.text[max(0, i - 200):i + 50] for i in [r.text.find(DOC)]]
    assert "[restricted]" in r.text


def test_scope_recheck_uses_the_same_filter(app_client, monkeypatch):
    monkeypatch.setenv("SCOPE_MIN_SIMILARITY", "0")    # report the closest chunk whatever its score
    from kestrel.access import TIER1, Principal
    from kestrel.graphs.chat import _scope_check
    tier1 = _scope_check({"masked": QUESTION, "principal": TIER1.as_state()})
    assert DOC not in tier1.get("documents", [])
    fraud = _scope_check({"masked": QUESTION,
                          "principal": Principal("f", frozenset({"all_staff", "fraud_operations"})).as_state()})
    assert DOC in fraud.get("documents", [])


def test_direct_document_lookup_applies_the_same_policy(app_client):
    from kestrel.access import TIER1, Principal
    from kestrel.tools.bank import policy_lookup
    denied = policy_lookup(policy_id=DOC, principal=TIER1)
    missing = policy_lookup(policy_id="KB-DOES-NOT-EXIST", principal=TIER1)
    assert not denied.ok and denied.error == f"policy {DOC} not found"       # same answer as a missing doc
    assert not missing.ok and missing.error.endswith("not found")
    ok = policy_lookup(policy_id=DOC, principal=Principal("f", frozenset({"all_staff", "fraud_operations"})))
    assert ok.ok and ok.data["policy_id"] == DOC
    assert policy_lookup(policy_id="KB-POL-LIAB-A", principal=TIER1).ok


def test_missing_principal_sees_nothing(app_client):
    """Fail closed: a graph state without a principal retrieves no chunks at all."""
    from kestrel.graphs.chat import retrieval
    out = retrieval({"run_id": "R-ACL", "thread_id": "S-ACL", "masked": "customer liability unauthorised card",
                     "sup": {"intent": "card_payment_not_recognised"}, "txn": None})
    assert out["hits"] == []


def test_transaction_date_filter_still_applies(app_client):
    from kestrel.access import TIER1
    from kestrel.db import connect
    from kestrel.ingest.embed import FakeEmbedder
    from kestrel.retrieval.search import vector_search
    q = FakeEmbedder().embed(["customer liability fraudulent electronic transaction reported within 5 calendar days"])[0]
    with connect() as conn:
        ids = [h.document_id for h in vector_search(conn, q, k=5, txn_date=date(2027, 1, 4), principal=TIER1)]
    assert "KB-POL-LIAB-B" in ids and "KB-POL-LIAB-A" not in ids


def test_ingest_rejects_missing_or_unknown_audience(tmp_path: Path):
    from kestrel.ingest.corpus import parse_document
    base = "---\nid: KB-X\ntitle: X\ndocument_type: policy\ndepartment: ops\nstatus: current\n{aud}---\nbody\n"
    for aud in ("", "audience: []\n", "audience: [fraud_ops]\n"):
        p = tmp_path / "x.md"
        p.write_text(base.format(aud=aud))
        with pytest.raises(ValueError):
            parse_document(p, tmp_path)
    p.write_text(base.format(aud="audience: [fraud_operations]\n"))
    assert parse_document(p, tmp_path).audience == ["fraud_operations"]
