from datetime import date

import pytest

from kestrel.config import get_settings
from kestrel.db import apply_schema, connect
from kestrel.ingest.embed import FakeEmbedder
from kestrel.ingest.run import ingest_banking77, ingest_corpus
from kestrel.ingest.seed import seed
from kestrel.retrieval.search import vector_search

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def loaded(db_url):
    settings = get_settings()
    emb = FakeEmbedder(settings.embedding_dim)
    with connect(db_url) as conn:
        apply_schema(conn)
        seed(conn, anchor=date(2026, 12, 2), force=True)
        ingest_banking77(conn, settings)
        first = ingest_corpus(conn, settings, emb)
        second = ingest_corpus(conn, settings, emb)
    return db_url, emb, first, second


def test_corpus_meets_brief_minimum_and_is_idempotent(loaded):
    _, _, first, second = loaded
    assert first["documents"] >= 25
    assert first["embedded"] == first["documents"]
    assert second["embedded"] == 0 and second["skipped"] == first["documents"]


def test_every_chunk_carries_required_metadata(loaded):
    url, *_ = loaded
    with connect(url) as conn:
        missing = conn.execute(
            """SELECT count(*) FROM document_chunks WHERE NOT (metadata ?& array
               ['document_type','department','sensitivity','source_path','status'])"""
        ).fetchone()[0]
    assert missing == 0


def test_liability_query_returns_current_policy_not_superseded(loaded):
    url, emb, *_ = loaded
    q = emb.embed(["customer liability for an unauthorised card transaction reported within three working days"])[0]
    with connect(url) as conn:
        hits = vector_search(conn, q, k=5, txn_date=date(2026, 12, 1))
    ids = [h.document_id for h in hits]
    assert ids[0] == "KB-POL-LIAB-A"
    assert "KB-POL-LIAB-2018" not in ids
    assert "KB-POL-LIAB-B" not in ids  # excluded by transaction date


def test_txn_date_in_2027_selects_new_regime(loaded):
    url, emb, *_ = loaded
    q = emb.embed(["customer liability fraudulent electronic transaction reported within 5 calendar days"])[0]
    with connect(url) as conn:
        hits = vector_search(conn, q, k=5, txn_date=date(2027, 1, 4))
    ids = [h.document_id for h in hits]
    assert ids[0] == "KB-POL-LIAB-B"
    assert "KB-POL-LIAB-A" not in ids


def test_superseded_doc_only_returned_when_asked(loaded):
    url, emb, *_ = loaded
    q = emb.embed(["Unauthorised Transactions Policy 2018 edition"])[0]
    with connect(url) as conn:
        assert "KB-POL-LIAB-2018" not in [h.document_id for h in vector_search(conn, q, k=10)]
        assert "KB-POL-LIAB-2018" in [h.document_id for h in vector_search(conn, q, k=10, include_superseded=True)]


def test_banking77_normalised_with_in_scope_flag(loaded):
    url, *_ = loaded
    with connect(url) as conn:
        total, scoped, intents = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE in_scope), count(DISTINCT intent_label) FROM banking_queries"
        ).fetchone()
    assert total == 13083 and intents == 77
    assert 0 < scoped < total


def test_hero_transaction_seeded(loaded):
    url, *_ = loaded
    with connect(url) as conn:
        row = conn.execute("SELECT amount_inr, txn_date FROM mock_transactions WHERE txn_id = 'T-HERO'").fetchone()
    assert row == (18450, date(2026, 12, 1))
