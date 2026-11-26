"""Read-only bank connector over MCP stdio (D57): discovery, typed calls, scope enforced in the
connector, and a connector timeout that ends in a clear failure with no financial draft."""

import os
import time
import uuid
from datetime import date

import pytest

from kestrel.access import TIER1

pytestmark = pytest.mark.db
ANCHOR = date(2026, 11, 24)
HERO = "Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, it went through yesterday"


@pytest.fixture(scope="module")
def env(db_url):
    old = dict(os.environ)
    os.environ.update({"DATABASE_URL": db_url, "LLM_PROVIDER": "fake", "EMBEDDING_PROVIDER": "fake",
                       "SEED_ANCHOR_DATE": ANCHOR.isoformat(), "RETRIEVAL_MIN_SCORE": "0", "TOOL_TRANSPORT": "mcp"})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
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
    yield
    from kestrel.connectors import mcp_client
    mcp_client.reset()
    os.environ.clear(); os.environ.update(old)


def token(**kw):
    from kestrel.connectors.scope import mint
    return mint(subject="agent1", **kw)


def test_client_discovers_both_read_only_tools_with_typed_schemas(env):
    from kestrel.connectors import mcp_client
    tools = {t.name: t for t in mcp_client.list_tools().tools}
    assert set(tools) == {"transaction_lookup", "ticket_lookup"}       # nothing that writes
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert "scope_token" in t.input_schema["required"]
        assert t.output_schema and t.output_schema["type"] == "object"
    assert "transactions" in tools["transaction_lookup"].output_schema["properties"]
    assert "tickets" in tools["ticket_lookup"].output_schema["properties"]


def test_in_scope_calls_return_typed_rows(env):
    from kestrel.connectors import mcp_client
    r = mcp_client.call("transaction_lookup", {"scope_token": token(accounts=["A1001"]), "account_id": "A1001",
                                                "as_of": ANCHOR.isoformat()})
    assert r.ok, r.error
    assert "T-HERO" in {t["txn_id"] for t in r.data["transactions"]}
    assert r.data["transactions"][0]["txn_date"].count("-") == 2          # dates typed, ISO on the wire
    t = mcp_client.call("ticket_lookup", {"scope_token": token(accounts=["A2001"]), "account_id": "A2001"})
    assert t.ok and [x["ticket_id"] for x in t.data["tickets"]] == ["TCK-2001"]


def test_connector_denies_out_of_scope_lookups(env):
    from kestrel.connectors import mcp_client
    a1001 = token(accounts=["A1001"], cards=["4821"])
    cases = [
        ("transaction_lookup", {"account_id": "A2001"}, "out_of_scope: account A2001"),
        ("transaction_lookup", {"card_last4": "7710"}, "out_of_scope: card ending 7710"),
        ("ticket_lookup", {"ticket_id": "TCK-2001"}, "out_of_scope: ticket TCK-2001"),
        ("ticket_lookup", {"account_id": "A3001"}, "out_of_scope: account A3001"),
    ]
    for tool, args, why in cases:
        r = mcp_client.call(tool, {"scope_token": a1001, **args})
        assert not r.ok and why in r.error, (tool, args, r)
    forged = a1001[:-4] + ("0000" if not a1001.endswith("0000") else "1111")
    r = mcp_client.call("transaction_lookup", {"scope_token": forged, "account_id": "A1001"})
    assert not r.ok and "unauthorized: scope token signature invalid" in r.error
    from kestrel.connectors.scope import mint
    r = mcp_client.call("transaction_lookup", {"scope_token": mint(subject="x", accounts=["A1001"], ttl_s=-1),
                                                "account_id": "A1001"})
    assert not r.ok and "expired" in r.error


def test_model_supplied_card_outside_typed_digits_is_refused(env):
    """The Supervisor LLM's card_last4 is a tool argument, not a permission: if the agent typed
    card 4821, a model saying 7710 (another customer) gets nothing."""
    from kestrel.graphs.chat import tool_exec
    st = {"run_id": "R-M", "thread_id": "S-M", "session_id": "S-M", "idv_level": 2, "entities": {},
          "masked": "Customer never made a payment on card ending 4821", "principal": TIER1.as_state(),
          "sup": {"intent": "card_payment_not_recognised", "card_last4": "7710", "amount_inr": None}}
    out = tool_exec(st)
    assert out["txn"] is None and "out_of_scope" in out["no_draft_reason"]
    assert out["tool_calls"][0]["via"] == "mcp" and out["tool_calls"][0]["ok"] is False


def test_hero_runs_through_the_connector(env):
    from kestrel.db import connect
    from kestrel.graphs.chat import stream_run
    with connect() as conn:
        conn.execute("UPDATE mock_cards SET status = 'active'"); conn.commit()
    run_id = f"RUN-{uuid.uuid4().hex[:8]}"
    events = list(stream_run(run_id, f"S-{uuid.uuid4().hex[:6]}", HERO, None, 2, principal=TIER1))
    final = next(e for e in events if e["type"] == "final")
    assert final["draft"] and final["draft"]["amount_inr"] == 18450
    with connect() as conn:
        calls = conn.execute("SELECT detail->'calls' FROM run_events WHERE run_id = %s AND node = 'tool_exec'",
                             (run_id,)).fetchone()[0]
    assert calls[0]["tool"] == "transaction_lookup" and calls[0]["via"] == "mcp" and calls[0]["ok"]


def test_connector_timeout_fails_clearly_and_drafts_nothing(env, monkeypatch):
    from kestrel.db import connect
    from kestrel.graphs.chat import stream_run
    monkeypatch.setenv("KESTREL_MCP_FAULT", "slow:transaction_lookup:10")   # the connector hangs
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_S", "1")
    with connect() as conn:
        conn.execute("UPDATE mock_cards SET status = 'active'"); conn.commit()
        drafts_before = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    t0 = time.perf_counter()
    events = list(stream_run(f"RUN-{uuid.uuid4().hex[:8]}", f"S-{uuid.uuid4().hex[:6]}", HERO, None, 2,
                             principal=TIER1))
    elapsed = time.perf_counter() - t0
    final = next(e for e in events if e["type"] == "final")
    assert final["draft"] is None and final["calc"] is None
    # The agent is told why (the UI shows no_draft_reason; the composer gets it in FACTS).
    assert final["no_draft_reason"].startswith("bank lookup failed (connector_timeout); no transaction data")
    assert elapsed < 8, elapsed                                            # bounded, not hung
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == drafts_before
        assert conn.execute("SELECT status FROM mock_cards WHERE card_id = 'K1001'").fetchone()[0] == "active"
    monkeypatch.delenv("KESTREL_MCP_FAULT")
    from kestrel.connectors import mcp_client
    r = mcp_client.call("transaction_lookup", {"scope_token": token(accounts=["A1001"]), "account_id": "A1001"})
    assert r.ok                                                            # next call gets a fresh connector
