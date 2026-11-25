"""FastAPI app: chat + SSE stream, drafts → cases, supervisor approvals, health, UI."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from kestrel import cases
from kestrel.config import get_settings
from kestrel.db import connect
from kestrel.guards.pii import mask_pii
from kestrel.tracing import flush as flush_traces, trace_id_for, trace_url

log = logging.getLogger("api")
UI = Path(__file__).resolve().parents[1] / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        from kestrel.graphs.case import reconcile
        from kestrel.graphs.chat import chat_graph
        chat_graph()
        log.info("startup reconcile: %s", reconcile())
    except Exception:  # noqa: BLE001 - /health reports it; don't crash-loop
        log.exception("startup reconcile failed")
    yield


app = FastAPI(title="Kestrel Resolution Copilot", version="0.2.0", lifespan=lifespan)

# Raw agent text waits here (in memory only) between POST /chat and GET /stream. It is
# never written anywhere unmasked; if the app restarts in those seconds the run is lost
# and the agent resends (D19).
_PENDING: dict[str, tuple[float, str, str, str | None, int]] = {}
_LOCK = threading.Lock()
PENDING_TTL_S = 120


def _role(x_role: str | None, *allowed: str) -> str:
    role = x_role or "agent"
    if role not in allowed:
        raise HTTPException(403, f"role {role!r} cannot do this (needs {' or '.join(allowed)})")
    return role


@app.get("/health")
def health() -> JSONResponse:
    """Readiness: database reachable, knowledge base loaded, model key configured."""
    s = get_settings()
    checks: dict[str, object] = {}
    try:
        with connect() as conn:
            docs, chunks, queries = conn.execute(
                "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks),"
                " (SELECT count(*) FROM banking_queries)"
            ).fetchone()
            model = conn.execute("SELECT min(metadata->>'embedding_model') FROM document_chunks").fetchone()[0]
        checks["database"] = "ok"
        checks["knowledge_base"] = {"documents": docs, "chunks": chunks, "banking77_queries": queries,
                                    "embedding_model": model}
        kb_ready = docs > 0 and chunks > 0
    except Exception as e:  # noqa: BLE001 - health must report, not raise
        checks["database"] = f"error: {type(e).__name__}"
        kb_ready = False
    checks["model_key"] = "configured" if s.openai_api_key else "missing"
    checks["embedding_provider"] = s.embedding_provider
    ready = checks["database"] == "ok" and kb_ready
    return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503)


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(UI)


# --- chat --------------------------------------------------------------------------

class ChatIn(BaseModel):
    message: str = Field(min_length=2, max_length=4000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    account_id: str | None = Field(default=None, pattern=r"^[A-Z0-9]{3,16}$")
    idv_level: int = Field(default=0, ge=0, le=3, description="Caller verification level the agent completed (KB-SOP-IDV)")


@app.post("/chat")
def chat(body: ChatIn, x_role: str | None = Header(default=None)) -> dict:
    _role(x_role, "agent", "supervisor")
    run_id = f"RUN-{uuid.uuid4().hex[:10]}"
    session_id = body.session_id or f"S-{uuid.uuid4().hex[:8]}"
    with connect() as conn:
        conn.execute("INSERT INTO chat_runs (run_id, session_id, message, trace_id) VALUES (%s, %s, %s, %s)",
                     (run_id, session_id, mask_pii(body.message).text, trace_id_for(run_id)))
        conn.commit()
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _PENDING.items() if now - v[0] > PENDING_TTL_S]:
            del _PENDING[k]
        _PENDING[run_id] = (now, session_id, body.message, body.account_id, body.idv_level)
    return {"run_id": run_id, "session_id": session_id, "thread_id": session_id, "stream": f"/stream/{run_id}"}


def _sse(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"


@app.get("/stream/{run_id}")
def stream(run_id: str) -> StreamingResponse:
    with _LOCK:
        pending = _PENDING.pop(run_id, None)
    if pending is None:
        raise HTTPException(404, "unknown or already-streamed run")
    _, session_id, message, account_id, idv_level = pending

    def gen():
        from kestrel.graphs.chat import stream_run
        _set_run(run_id, "running")
        final: dict = {}
        t0 = time.perf_counter()
        yield _sse({"type": "start", "run_id": run_id, "session_id": session_id})
        try:
            for ev in stream_run(run_id, session_id, message, account_id, idv_level):
                if ev.get("type") == "final":
                    final = ev
                yield _sse(ev)
            ms = int((time.perf_counter() - t0) * 1000)
            _set_run(run_id, "done", {**final, "latency_ms": ms})
            flush_traces()
            # Summary refresh is a background job, never on the hot path (design §5.1).
            from kestrel.jobs import queue
            queue.enqueue("memory_summary", {"session_id": session_id}, requested_by="chat",
                          dedupe_key=f"memory_summary:{session_id}")
            yield _sse({"type": "done", "run_id": run_id, "latency_ms": ms, "trace_url": trace_url(run_id)})
        except Exception as e:  # noqa: BLE001 - surface, don't hang the stream
            log.exception("run %s failed", run_id)
            _set_run(run_id, "failed", {"error": repr(e)[:500]})
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _set_run(run_id: str, state: str, result: dict | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE chat_runs SET state = %s, result = COALESCE(%s, result), updated_at = now() WHERE run_id = %s",
                     (state, Jsonb(result) if result is not None else None, run_id))
        conn.commit()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT run_id, session_id, message, state, result, created_at FROM chat_runs WHERE run_id = %s",
                         (run_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "run not found")
        ev = conn.execute("""SELECT node, status, latency_ms, tokens_in, tokens_out, cost_inr, detail, created_at
                             FROM run_events WHERE run_id = %s ORDER BY event_id""", (run_id,)).fetchall()
    return {"run_id": r[0], "session_id": r[1], "message_masked": r[2], "state": r[3], "result": r[4],
            "created_at": r[5], "trace_url": trace_url(run_id), "events": [_event(e) for e in ev], **_totals(ev)}


def _event(e) -> dict:
    return {"node": e[0], "status": e[1], "latency_ms": e[2], "tokens_in": e[3], "tokens_out": e[4],
            "cost_inr": float(e[5]) if e[5] is not None else None, "detail": e[6], "at": e[7]}


def _totals(ev) -> dict:
    return {"total_cost_inr": round(sum(float(e[5] or 0) for e in ev), 4),
            "total_tokens": sum((e[3] or 0) + (e[4] or 0) for e in ev)}


# --- drafts, cases, approvals --------------------------------------------------------

@app.post("/drafts/{draft_id}/submit")
def submit(draft_id: str, x_role: str | None = Header(default=None)) -> dict:
    _role(x_role, "agent")
    try:
        out = cases.submit_draft(draft_id)
        flush_traces()
        return out
    except cases.NotFound:
        raise HTTPException(404, "draft not found")


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    try:
        c = cases.get_case(case_id)
    except cases.NotFound:
        raise HTTPException(404, "case not found")
    with connect() as conn:
        ev = conn.execute("""SELECT node, status, latency_ms, tokens_in, tokens_out, cost_inr, detail, created_at
                             FROM run_events WHERE run_id = %s ORDER BY event_id""", (case_id,)).fetchall()
    return {**c, "trace_url": trace_url(case_id), "events": [_event(e) for e in ev], **_totals(ev)}


@app.get("/approvals")
def list_approvals(state: Literal["pending"] = "pending", x_role: str | None = Header(default=None)) -> list[dict]:
    _role(x_role, "supervisor")
    return cases.pending_approvals()


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=500)
    approver_id: str = Field(default="P01", pattern=r"^P\d{2}$")


@app.post("/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionIn, x_role: str | None = Header(default=None)):
    _role(x_role, "supervisor")
    if body.decision == "reject" and not body.reason:
        raise HTTPException(422, "a rejection needs a reason")
    try:
        out = cases.decide(approval_id, body.decision, body.approver_id, body.reason)
        flush_traces()
        return out
    except cases.NotFound:
        raise HTTPException(404, "approval not found")
    except cases.Forbidden as f:
        raise HTTPException(403, str(f))
    except cases.Conflict as c:
        return JSONResponse({"detail": "approval already decided", "existing": c.existing}, status_code=409)


# --- async jobs ------------------------------------------------------------------------

class JobIn(BaseModel):
    job_type: Literal["enron_compliance_scan", "tat_sweep"]
    payload: dict = Field(default_factory=dict)


@app.post("/jobs", status_code=202)
def create_job(body: JobIn, x_role: str | None = Header(default=None)) -> dict:
    """Returns immediately with a job id; the worker container runs it."""
    from kestrel.jobs import queue
    role = _role(x_role, "compliance", "supervisor")
    if body.job_type == "enron_compliance_scan" and role != "compliance":
        raise HTTPException(403, "only the compliance role can start a compliance scan")
    allowed = {"enron_compliance_scan": {"limit"}, "tat_sweep": {"as_of"}}[body.job_type]
    if set(body.payload) - allowed:   # e.g. no file paths from the client
        raise HTTPException(422, f"{body.job_type} accepts only {sorted(allowed)}")
    return queue.enqueue(body.job_type, body.payload, requested_by=role)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    from kestrel.jobs import queue
    try:
        return queue.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
