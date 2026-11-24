"""Vector search over document_chunks with metadata filters.

Milestone 1 provides plain vector search; hybrid search (full-text + vector with rank
fusion) is added for the retrieval ablation in Milestone 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg


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
    k: int = 5,
    include_superseded: bool = False,
    departments: list[str] | None = None,
    document_types: list[str] | None = None,
    txn_date: date | None = None,
) -> list[Hit]:
    where = ["TRUE"]
    params: dict[str, Any] = {"q": query_vec, "k": k}
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
