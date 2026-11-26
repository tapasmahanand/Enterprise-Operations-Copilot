"""D58 side-effect check: the fee-related items of evals/scope_set.yaml through the full chat graph, in a
separate database (kestrel_scope) so it can run beside `make eval`. Same repeats as scope_eval. ~₹40.

    python -m evals.fee_scope_check reports/fee-scope-after.json
"""
from __future__ import annotations

import json
import os
import re
import sys

import psycopg
import yaml

from evals.run_eval import ANCHOR, CONFIGS, ROOT

FEE_WORDS = re.compile(r"fee|revers|waive|charge|surcharge|penalty", re.I)


def setup_scope_db() -> None:
    admin = os.environ.get("DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel")
    base = admin.rpartition("/")[0]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS kestrel_scope WITH (FORCE)")
        c.execute("CREATE DATABASE kestrel_scope")
    os.environ.update({"DATABASE_URL": f"{base}/kestrel_scope", "SEED_ANCHOR_DATE": ANCHOR.isoformat(),
                       "LLM_PROVIDER": "openai", "EMBEDDING_PROVIDER": os.environ.get("EMBEDDING_PROVIDER", "openai")})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    from kestrel.config import get_settings
    from kestrel.db import apply_schema, connect
    from kestrel.ingest.embed import make_embedder
    from kestrel.ingest.run import ingest_corpus
    from kestrel.ingest.seed import seed
    with connect() as conn:
        apply_schema(conn)
        seed(conn, anchor=ANCHOR, force=True)
        ingest_corpus(conn, get_settings(), make_embedder(get_settings()))


def main(out: str) -> None:
    setup_scope_db()
    from evals.scope_eval import PLAN, classify
    items = [i for i in yaml.safe_load((ROOT / "evals" / "scope_set.yaml").read_text())["items"]
             if FEE_WORDS.search(i["note"])]
    res: dict[str, list] = {}
    for c, reps in PLAN.items():
        os.environ.update({k: v for k, v in CONFIGS[c].items() if k.endswith("_MODEL") or k == "MODEL_PROFILE"})
        runs = []
        for r in range(reps):
            for it in items:
                x = classify(it) | {"repeat": r}
                runs.append(x)
                print(c, "ok " if x["ok"] else "BAD", x["id"], x["label"], x["route"], x["intent"],
                      x["code_refusal"], x["draft"], flush=True)
        res[c] = runs
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "reports" / "fee-scope-after.json"))
