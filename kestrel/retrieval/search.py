"""Vector search over document_chunks with metadata filters.

Access control (D54): every search takes the reader's Principal, and the permission test
(`c.audience && groups`) is in the same WHERE clause as the status and transaction-date
filters, ahead of the ORDER BY on vector distance. A chunk the reader may not see is never
ranked into the LIMIT and never leaves Postgres. There is no default: a caller that forgets
the principal gets a TypeError, not an unfiltered search.

Caveat: with the HNSW index, pgvector applies WHERE filters to the index's candidate list
(ef_search, 40 by default), so a very selective filter can return fewer than k rows. That
loses recall, never leaks. On this 30-document corpus the planner scans the table anyway.

Milestone 1 provides plain vector search; hybrid search (full-text + vector with rank
fusion) is added for the retrieval ablation in Milestone 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from kestrel.access import Principal


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    title: str
    source_path: str
    score: float           # cosine similarity, 1 = identical
    text: str
    metadata: dict[str, Any]


def vector_search(
    conn: psycopg.Connection,
    query_vec: list[float],
    *,
    principal: Principal,
    k: int = 5,
    include_superseded: bool = False,
    departments: list[str] | None = None,
    document_types: list[str] | None = None,
    txn_date: date | None = None,
) -> list[Hit]:
    params: dict[str, Any] = {"q": query_vec, "k": k, "groups": sorted(principal.groups)}
    where = ["c.audience && %(groups)s::text[]"]
    if not include_superseded:
        where.append("c.metadata->>'status' = 'current'")
    if departments:
        where.append("c.metadata->>'department' = ANY(%(departments)s)")
        params["departments"] = departments
    if document_types:
        where.append("c.metadata->>'document_type' = ANY(%(types)s)")
        params["types"] = document_types
    if txn_date:
        # Keep date-scoped policies only when they cover the transaction date.
        where.append("""(c.metadata->>'applies_to_txn_before' IS NULL OR %(txn)s::date < (c.metadata->>'applies_to_txn_before')::date)
                        AND (c.metadata->>'applies_to_txn_on_or_after' IS NULL OR %(txn)s::date >= (c.metadata->>'applies_to_txn_on_or_after')::date)""")
        params["txn"] = txn_date
    sql = f"""
        SELECT c.chunk_id, c.document_id, d.title, d.source_path,
               1 - (c.embedding <=> %(q)s::vector) AS score, c.chunk_text, c.metadata
        FROM document_chunks c JOIN documents d USING (document_id)
        WHERE {' AND '.join(where)}
        ORDER BY c.embedding <=> %(q)s::vector
        LIMIT %(k)s"""
    rows = conn.execute(sql, params).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5], r[6]) for r in rows]


def _filters(principal: Principal, include_superseded: bool, txn_date: date | None,
             params: dict[str, Any]) -> list[str]:
    params["groups"] = sorted(principal.groups)
    where = ["c.audience && %(groups)s::text[]"]
    if not include_superseded:
        where.append("c.metadata->>'status' = 'current'")
    if txn_date:
        where.append("""(c.metadata->>'applies_to_txn_before' IS NULL OR %(txn)s::date < (c.metadata->>'applies_to_txn_before')::date)
                        AND (c.metadata->>'applies_to_txn_on_or_after' IS NULL OR %(txn)s::date >= (c.metadata->>'applies_to_txn_on_or_after')::date)""")
        params["txn"] = txn_date
    return where


def hybrid_search(
    conn: psycopg.Connection,
    query_vec: list[float],
    query_text: str,
    *,
    principal: Principal,
    k: int = 5,
    include_superseded: bool = False,
    txn_date: date | None = None,
    pool: int = 20,
    rrf_k: int = 60,
) -> list[Hit]:
    """Vector + Postgres full-text search, fused with reciprocal rank fusion
    (score = Σ 1/(rrf_k + rank)). Full text uses websearch_to_tsquery with OR between
    terms, so a query need not match every word. Used by the retrieval ablation (M4);
    production keeps vector search, see reports/eval.md for why."""
    params: dict[str, Any] = {"q": query_vec, "pool": pool,
                              "t": " OR ".join(w for w in query_text.replace('"', " ").split() if len(w) > 2)}
    where = " AND ".join(_filters(principal, include_superseded, txn_date, params))
    sql = f"""
        WITH v AS (
            SELECT c.chunk_id, row_number() OVER (ORDER BY c.embedding <=> %(q)s::vector) AS r
            FROM document_chunks c WHERE {where}
            ORDER BY c.embedding <=> %(q)s::vector LIMIT %(pool)s),
        f AS (
            SELECT c.chunk_id, row_number() OVER (ORDER BY ts_rank_cd(to_tsvector('english', c.chunk_text),
                     websearch_to_tsquery('english', %(t)s)) DESC) AS r
            FROM document_chunks c
            WHERE {where} AND to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery('english', %(t)s)
            LIMIT %(pool)s),
        fused AS (
            SELECT chunk_id, sum(1.0 / ({rrf_k} + r)) AS s FROM (SELECT * FROM v UNION ALL SELECT * FROM f) u
            GROUP BY chunk_id)
        SELECT c.chunk_id, c.document_id, d.title, d.source_path, fused.s, c.chunk_text, c.metadata
        FROM fused JOIN document_chunks c USING (chunk_id) JOIN documents d USING (document_id)
        ORDER BY fused.s DESC LIMIT {int(k)}"""
    rows = conn.execute(sql, params).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5], r[6]) for r in rows]
