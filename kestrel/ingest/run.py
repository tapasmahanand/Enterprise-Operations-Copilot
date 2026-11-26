"""One-shot ingest: schema → mock data → BANKING77 → corpus chunks + embeddings.

Idempotent: documents whose content hash is unchanged are skipped, so re-running
`docker compose up` does not re-embed (or re-bill) anything.

    python -m kestrel.ingest.run [--skip-embeddings-check]
"""

from __future__ import annotations

import json
import logging
import sys

from psycopg.types.json import Jsonb

from kestrel.config import get_settings
from kestrel.db import apply_schema, connect
from kestrel.ingest.banking77 import normalise
from kestrel.ingest.corpus import chunk_document, load_corpus
from kestrel.ingest.embed import make_embedder
from kestrel.ingest.seed import seed

log = logging.getLogger("ingest")


def ingest_banking77(conn, settings) -> int:
    rows = list(normalise(settings.banking77_dir))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO banking_queries (query_id, split, user_query, intent_label, in_scope, priority, department, risk_level)
               VALUES (%(query_id)s, %(split)s, %(user_query)s, %(intent_label)s, %(in_scope)s, %(priority)s, %(department)s, %(risk_level)s)
               ON CONFLICT (query_id) DO UPDATE SET in_scope = EXCLUDED.in_scope, priority = EXCLUDED.priority,
                 department = EXCLUDED.department, risk_level = EXCLUDED.risk_level""",
            rows,
        )
    conn.commit()
    return len(rows)


def ingest_corpus(conn, settings, embedder) -> dict[str, int]:
    docs = load_corpus(settings.corpus_dir)
    stats = {"documents": len(docs), "embedded": 0, "skipped": 0, "chunks": 0}
    # Skip only when both the content and the embedding model are unchanged; switching
    # from the fake embedder to OpenAI must re-embed everything.
    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        """SELECT d.document_id, d.content_hash, min(c.metadata->>'embedding_model')
           FROM documents d LEFT JOIN document_chunks c USING (document_id) GROUP BY 1, 2""").fetchall()}
    for doc in docs:
        if existing.get(doc.document_id) == (doc.content_hash, embedder.model):
            # Text unchanged, so no re-embedding; but an audience change must still apply (D54).
            conn.execute("UPDATE documents SET audience = %s WHERE document_id = %s AND audience IS DISTINCT FROM %s",
                         (doc.audience, doc.document_id, doc.audience))
            conn.execute("""UPDATE document_chunks SET audience = %s,
                                metadata = jsonb_set(metadata, '{audience}', to_jsonb(%s::text[]))
                            WHERE document_id = %s AND audience IS DISTINCT FROM %s""",
                         (doc.audience, doc.audience, doc.document_id, doc.audience))
            stats["skipped"] += 1
            continue
        chunks = chunk_document(doc)
        vectors = embedder.embed([c.text for c in chunks])
        with conn.transaction():
            conn.execute("DELETE FROM documents WHERE document_id = %s", (doc.document_id,))
            conn.execute(
                """INSERT INTO documents (document_id, title, source_path, document_type, department, sensitivity,
                                          effective_from, status, supersedes, content_hash, audience)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (doc.document_id, doc.title, doc.source_path, doc.document_type, doc.department, doc.sensitivity,
                 doc.effective_from, doc.status, doc.supersedes, doc.content_hash, doc.audience),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO document_chunks (chunk_id, document_id, chunk_index, chunk_text, token_count, metadata,
                                                     embedding, audience)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(c.chunk_id, c.document_id, c.chunk_index, c.text, c.token_count,
                      Jsonb({**c.metadata, "embedding_model": embedder.model}), v, doc.audience)
                     for c, v in zip(chunks, vectors)],
                )
        stats["embedded"] += 1
        stats["chunks"] += len(chunks)
    # Remove documents deleted from the corpus folder.
    ids = [d.document_id for d in docs]
    conn.execute("DELETE FROM documents WHERE NOT (document_id = ANY(%s))", (ids,))
    conn.commit()
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    embedder = make_embedder(settings)
    with connect() as conn:
        apply_schema(conn)
        report = {
            "mock": seed(conn),
            "banking77": ingest_banking77(conn, settings),
            "corpus": ingest_corpus(conn, settings, embedder),
            "embedding_model": embedder.model,
        }
    log.info("ingest complete: %s", json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
