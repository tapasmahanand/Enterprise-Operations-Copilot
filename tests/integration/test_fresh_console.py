"""A fresh console session (level 0, as the UI and API default) cannot get money moved:
no card block, no credit draft, so nothing can be sent to a supervisor. Goes through the
HTTP API exactly as the console does (POST /chat, then the SSE stream)."""

import json
import os
from datetime import date

import pytest

pytestmark = pytest.mark.db
ANCHOR = date(2026, 11, 24)
HERO = "Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, it went through yesterday"
TWICE = "Customer says she was charged twice at Urban Threads on her card ending 7710"


@pytest.fixture(scope="module")
def client(db_url):
    old = dict(os.environ)
    os.environ.update({"DATABASE_URL": db_url, "LLM_PROVIDER": "fake", "EMBEDDING_PROVIDER": "fake",
                       "SEED_ANCHOR_DATE": ANCHOR.isoformat(), "RETRIEVAL_MIN_SCORE": "0"})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
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


def _ask(client, payload):
    r = client.post("/chat", json=payload, headers={"X-Role": "agent"})
    assert r.status_code == 200
    events = []
    with client.stream("GET", r.json()["stream"]) as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return r.json(), events


def _drafts_and_blocks():
    from kestrel.db import connect
    with connect() as conn:
        return (conn.execute("SELECT count(*) FROM drafts").fetchone()[0],
                conn.execute("SELECT count(*) FROM mock_cards WHERE status = 'blocked'").fetchone()[0])


@pytest.mark.parametrize("message", [HERO, TWICE])
def test_fresh_session_cannot_refund(client, message):
    before = _drafts_and_blocks()
    # A fresh console sends no session id; the level is whatever the UI starts at (0).
    _, events = _ask(client, {"message": message})
    final = next(e for e in events if e["type"] == "final")
    assert final["draft"] is None, "no credit may be drafted for an unverified caller"
    assert not any(e["type"] == "draft" for e in events)
    assert (final["card_block"] or {}).get("status") != "blocked"
    assert _drafts_and_blocks() == before
    assert "Send to supervisor" not in final["answer"]
    assert final["calc"] is None, "no account lookup or calculation for an unverified caller"
    assert "T-HERO" not in final["answer"] and "T2002" not in final["answer"]


def test_level_is_recorded_in_the_trace(client):
    meta, _ = _ask(client, {"message": HERO})
    run = client.get(f"/runs/{meta['run_id']}").json()
    ci = next(e for e in run["events"] if e["node"] == "compliance_in")
    assert ci["detail"]["idv_level"] == 0


def test_same_call_after_level_2_drafts_and_records_level(client):
    from kestrel.db import connect
    with connect() as conn:
        conn.execute("UPDATE mock_cards SET status = 'active'"); conn.commit()
    _, events = _ask(client, {"message": HERO, "idv_level": 2})
    final = next(e for e in events if e["type"] == "final")
    assert final["draft"]["amount_inr"] == 18450 and final["draft"]["idv_level"] == 2
