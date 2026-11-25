"""Session summary refresh, run by the worker after a chat run (off the hot path).

Reads the session's recent masked notes and answers, asks a small model for a 2-3
sentence summary, and stores it on `sessions.summary`; memory_load hands it to the
Supervisor on the next message. Entities (account, card, txn, draft) are kept by
memory_save in code; this adds the narrative ("customer already confirmed no OTP shared")."""

from __future__ import annotations

from typing import Any, Callable

from kestrel.db import connect
from kestrel.llm import get_llm, model_for

SYSTEM = ("Summarise this bank call so far in at most 3 short sentences for the agent's copilot: the customer's "
          "problem, what was already done (blocks, drafts, cases) and what is still open. Use only the text given. "
          "Never include card numbers or personal data.")


def run(payload: dict[str, Any], report: Callable[[dict], None]) -> dict[str, Any]:
    sid = payload["session_id"]
    with connect() as conn:
        rows = conn.execute(
            """SELECT message, result->>'answer' FROM chat_runs WHERE session_id = %s AND state = 'done'
               ORDER BY created_at DESC LIMIT 6""", (sid,)).fetchall()
    if not rows:
        return {"session_id": sid, "skipped": "no finished runs"}
    convo = "\n".join(f"AGENT NOTE: {m}\nCOPILOT: {(a or '')[:600]}" for m, a in reversed(rows))
    text, usage = get_llm().text(SYSTEM, convo, model=model_for("rewrite"))
    from kestrel.guards.pii import mask_pii
    summary = mask_pii(text.strip()).text[:800]
    with connect() as conn:
        conn.execute("""INSERT INTO sessions (session_id, user_id, summary) VALUES (%s, 'agent', %s)
                        ON CONFLICT (session_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = now()""",
                     (sid, summary))
        conn.commit()
    return {"session_id": sid, "summary": summary, "runs_used": len(rows), **usage.trace()}
