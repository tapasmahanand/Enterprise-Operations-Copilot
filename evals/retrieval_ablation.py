"""Retrieval ablation: vector vs hybrid (vector + full text, RRF) vs each with metadata
filters (current status + transaction-date scope), on evals/retrieval_gold.yaml.

Ranks *documents* (best chunk per document), because the gold labels are documents and
most documents are a single chunk. Queries are embedded as written (no LLM rewrite), so
this isolates the retrieval method. The production path adds the rewrite and a +0.1
intent-policy boost on top of "vector + filters".

    python -m evals.retrieval_ablation   # -> printed table + returned dict (used by run_eval)
"""

from __future__ import annotations

from datetime import date

import yaml

from kestrel.config import ROOT, get_settings
from kestrel.db import connect
from kestrel.ingest.embed import make_embedder
from kestrel.retrieval.search import hybrid_search, vector_search

GOLD = ROOT / "evals" / "retrieval_gold.yaml"
VARIANTS = {
    "vector (no filters)": dict(method="vector", filters=False),
    "hybrid (no filters)": dict(method="hybrid", filters=False),
    "vector + filters (production base)": dict(method="vector", filters=True),
    "hybrid + filters": dict(method="hybrid", filters=True),
}


def _docs(hits) -> list[str]:
    seen: list[str] = []
    for h in hits:
        if h.document_id not in seen:
            seen.append(h.document_id)
    return seen


def _wrong_version(doc: str, txn: date | None, meta: dict[str, dict]) -> bool:
    m = meta[doc]
    if m.get("status") == "superseded":
        return True
    if txn is None:
        return False
    before, after = m.get("applies_to_txn_before"), m.get("applies_to_txn_on_or_after")
    return bool((before and txn >= date.fromisoformat(before)) or (after and txn < date.fromisoformat(after)))


def run(k: int = 5) -> dict:
    gold = yaml.safe_load(GOLD.read_text())
    emb = make_embedder(get_settings())
    vecs = emb.embed([g["q"] for g in gold])
    out = {}
    with connect() as conn:
        meta = {r[0]: r[1] for r in conn.execute(
            "SELECT DISTINCT ON (document_id) document_id, metadata FROM document_chunks ORDER BY document_id, chunk_id")}
        for name, v in VARIANTS.items():
            r1 = r3 = r5 = mrr = 0.0
            wrong3 = nver = 0
            misses = []
            for g, vec in zip(gold, vecs):
                txn = g.get("txn_date")
                kw = dict(include_superseded=not v["filters"], txn_date=txn if v["filters"] else None)
                hits = (vector_search(conn, vec, k=20, **kw) if v["method"] == "vector"
                        else hybrid_search(conn, vec, g["q"], k=20, **kw))
                docs = _docs(hits)[:k]
                rel = set(g["relevant"])
                rank = next((i + 1 for i, d in enumerate(docs) if d in rel), None)
                r1 += rank == 1; r3 += bool(rank and rank <= 3); r5 += bool(rank and rank <= 5)
                mrr += 1 / rank if rank else 0
                if g.get("versioned"):
                    nver += 1
                    wrong3 += any(_wrong_version(d, txn, meta) for d in docs[:3])
                if rank != 1:
                    misses.append({"q": g["q"], "top3": docs[:3], "want": g["relevant"]})
            n = len(gold)
            out[name] = {"n": n, "recall@1": r1 / n, "recall@3": r3 / n, "recall@5": r5 / n, "mrr": mrr / n,
                         "wrong_version@3": wrong3 / nver if nver else 0.0, "n_versioned": nver, "misses": misses}
    return out


def table(res: dict) -> str:
    rows = ["| Variant | Recall@1 | Recall@3 | Recall@5 | MRR | Wrong policy version in top 3 |", "|---|---|---|---|---|---|"]
    for name, r in res.items():
        rows.append(f"| {name} | {r['recall@1']:.0%} | {r['recall@3']:.0%} | {r['recall@5']:.0%} | {r['mrr']:.2f} | "
                    f"{r['wrong_version@3']:.0%} of {r['n_versioned']} |")
    return "\n".join(rows)


if __name__ == "__main__":
    res = run()
    print(table(res))
    for name, r in res.items():
        print(f"\n{name} misses@1:")
        for m in r["misses"]:
            print("  ", m)
