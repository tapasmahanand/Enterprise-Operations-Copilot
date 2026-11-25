"""Reproduce the M2 incident for the postmortem: with and without the composer prompt's
list of allowed chunk ids, how often does the raw answer cite a chunk that wasn't
retrieved (and get withheld by the output check)?

    python -m evals.repro_invented_citation 20      # runs against DATABASE_URL (demo DB)
"""

from __future__ import annotations

import re
import sys
import uuid

from kestrel.guards.output import _CITE

ALLOWED = re.compile(r"\n\nYou may cite ONLY these chunk ids, copied exactly: .*$", re.S)


def trial(n: int, with_fix: bool, drop: str | None = None) -> dict:
    """drop: a chunk id removed from retrieval results, recreating the incident's condition
    (a sibling chunk of the same policy retrieved, this one not)."""
    import kestrel.graphs.chat as chat
    import kestrel.llm as llm
    orig_search = chat.vector_search
    if drop:
        chat.vector_search = lambda *a, **k: [h for h in orig_search(*a, **k) if h.chunk_id != drop]
    from kestrel.db import connect
    from kestrel.graphs.chat import chat_graph, stream_run
    base = llm.get_llm()
    orig = type(base).stream

    def stream(self, system, user, *, model):
        return orig(self, system, user if with_fix else ALLOWED.sub("", user), model=model)

    type(base).stream = stream
    hero = "Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, it went through yesterday."
    invented, withheld, examples = 0, 0, []
    try:
        for _ in range(n):
            with connect() as conn:
                conn.execute("UPDATE mock_cards SET status = 'active'"); conn.commit()
            session = f"S-REPRO-{uuid.uuid4().hex[:6]}"
            raw, final = [], None
            for ev in stream_run(f"REPRO-{uuid.uuid4().hex[:8]}", session, hero, None, 2):
                if ev["type"] == "token":
                    raw.append(ev["text"])
                elif ev["type"] == "final":
                    final = ev
            hits = {h["chunk_id"] for h in chat_graph().get_state({"configurable": {"thread_id": session}}).values["hits"]}
            bad = set(_CITE.findall("".join(raw))) - hits
            invented += bool(bad)
            withheld += final["output_check"]["verdict"] == "block"
            if bad and len(examples) < 3:
                examples.append({"cited_not_retrieved": sorted(bad), "retrieved": sorted(hits)})
    finally:
        type(base).stream = orig
        chat.vector_search = orig_search
    return {"with_fix": with_fix, "dropped": drop, "n": n, "invented": invented, "withheld": withheld, "examples": examples}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    for drop in (None, "KB-POL-LIAB-A:0"):
        for fix in (False, True):
            print(trial(n, fix, drop), flush=True)
