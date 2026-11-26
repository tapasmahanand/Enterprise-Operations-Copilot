"""FastAPI app: chat + SSE stream, drafts → cases, supervisor approvals, health, UI."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from kestrel import auth, cases
from kestrel.access import principal_for, redact_chunk_refs, visible_document_ids
from kestrel.auth import User, audit, current_user, require
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
_PENDING: dict[str, tuple[float, str, str, str | None, int, str]] = {}
_LOCK = threading.Lock()
PENDING_TTL_S = 120


COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"   # set to 1 behind HTTPS


# --- login ---------------------------------------------------------------------------------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    """Checks the password, creates a server-side session and sets it as an HttpOnly cookie.
    The token is also returned for scripts, which send it as `Authorization: Bearer`."""
    ip = request.client.host if request.client else None
    try:
        out = auth.login(body.username, body.password, ip)
    except auth.TooManyAttempts:
        raise HTTPException(429, "too many failed sign-ins; try again in 15 minutes")
    if out is None:
        raise HTTPException(401, "wrong username or password")
    token, user = out
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_TTL_S, httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, path="/")
    return {"user": user.public(), "token": token, "expires_in_s": auth.SESSION_TTL_S}


@app.post("/auth/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user)) -> dict:
    auth.logout(auth.token_from(request), user)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"signed_out": user.user_id}


@app.get("/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return user.public()


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
    from kestrel.llm import model_for, model_profile
    checks["models"] = {"profile": model_profile(), "supervisor": model_for("supervisor"),
                        "composer": model_for("composer")}
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
def chat(body: ChatIn, user: User = Depends(current_user)) -> dict:
    require(user, "agent", "supervisor")
    run_id = f"RUN-{uuid.uuid4().hex[:10]}"
    session_id = body.session_id or f"S-{uuid.uuid4().hex[:8]}"
    with connect() as conn:
        # A call session belongs to the user who started it: another user can't continue it and
        # inherit its memory (entities, summary) or its drafts.
        owner = conn.execute("SELECT user_id FROM chat_runs WHERE session_id = %s ORDER BY created_at LIMIT 1",
                             (session_id,)).fetchone()
        if owner and owner[0] != user.user_id:
            raise HTTPException(403, "this call session belongs to another user")
        conn.execute("""INSERT INTO chat_runs (run_id, session_id, message, trace_id, user_id, idv_level)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                     (run_id, session_id, mask_pii(body.message).text, trace_id_for(run_id), user.user_id,
                      body.idv_level))
        # The identity-verification level is the agent's own attestation, not checked by the
        # server (D51). What changed: it is now recorded against a signed-in user.
        audit(conn, user, "chat", "run", run_id, {"session_id": session_id, "idv_level_attested": body.idv_level,
                                                 "account_id": body.account_id})
        conn.commit()
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _PENDING.items() if now - v[0] > PENDING_TTL_S]:
            del _PENDING[k]
        _PENDING[run_id] = (now, session_id, body.message, body.account_id, body.idv_level, user.user_id)
    return {"run_id": run_id, "session_id": session_id, "thread_id": session_id, "stream": f"/stream/{run_id}"}


def _sse(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"


@app.get("/stream/{run_id}")
def stream(run_id: str, user: User = Depends(current_user)) -> StreamingResponse:
    with _LOCK:
        pending = _PENDING.get(run_id)
        if pending is not None and pending[5] != user.user_id:
            pending = None          # someone else's run: same answer as an unknown one
        else:
            _PENDING.pop(run_id, None)
    if pending is None:
        raise HTTPException(404, "unknown or already-streamed run")
    _, session_id, message, account_id, idv_level, _owner = pending
    principal = principal_for(user)   # documents are filtered by the signed-in user's groups (D54)

    def gen():
        from kestrel.graphs.chat import stream_run
        _set_run(run_id, "running")
        final: dict = {}
        t0 = time.perf_counter()
        yield _sse({"type": "start", "run_id": run_id, "session_id": session_id})
        try:
            for ev in stream_run(run_id, session_id, message, account_id, idv_level, principal=principal):
                if ev.get("type") == "final":
                    final = ev
                yield _sse(ev)
            ms = int((time.perf_counter() - t0) * 1000)
            _set_run(run_id, "done", {**final, "latency_ms": ms})
            flush_traces()
            # Summary refresh is a background job, never on the hot path (design §5.1).
            from kestrel.jobs import queue
            try:
                queue.enqueue("memory_summary", {"session_id": session_id}, requested_by="chat",
                              dedupe_key=f"memory_summary:{session_id}")
            except Exception:  # noqa: BLE001 - a failed summary refresh must not fail a completed run
                log.exception("memory_summary enqueue failed for session %s", session_id)
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
def get_run(run_id: str, user: User = Depends(current_user)) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT run_id, session_id, message, state, result, created_at, user_id FROM chat_runs"
                         " WHERE run_id = %s", (run_id,)).fetchone()
        if r is None or (r[6] != user.user_id and user.role != "supervisor"):
            raise HTTPException(404, "run not found")
        ev = conn.execute("""SELECT node, status, latency_ms, tokens_in, tokens_out, cost_inr, detail, created_at
                             FROM run_events WHERE run_id = %s ORDER BY event_id""", (run_id,)).fetchall()
        events, result = [_event(e) for e in ev], r[4]
        if r[6] != user.user_id:
            # A supervisor reading an agent's run sees only the documents they may read (D54).
            events, result = _redact_run(conn, principal_for(user), events, result)
    return {"run_id": r[0], "session_id": r[1], "message_masked": r[2], "state": r[3], "result": result,
            "created_at": r[5], "trace_url": trace_url(run_id), "events": events, **_totals(ev)}


_KB_REF = re.compile(r"\bKB-[A-Z0-9-]+[A-Z0-9](?::\d+)?")


def _redact_run(conn, viewer, events: list[dict], result: dict | None) -> tuple[list[dict], dict | None]:
    """Replace every document or chunk id the viewer can't read, wherever it appears in a run's
    trace rows and stored result (retrieval detail, output check, citations, answer text)."""
    blob = json.dumps({"events": events, "result": result}, default=str)
    refs = set(_KB_REF.findall(blob))
    docs = {r.split(":", 1)[0] for r in refs}
    ok_docs = visible_document_ids(conn, viewer, docs)
    # Rule ids such as KB-REF-1 look like document ids but name no document: leave them.
    real = {d for (d,) in conn.execute("SELECT document_id FROM documents WHERE document_id = ANY(%s)",
                                        (list(docs),)).fetchall()}
    for ref in sorted(refs, key=len, reverse=True):     # chunk ids before their document id
        if ref.split(":", 1)[0] in real - ok_docs:
            blob = blob.replace(ref, "[restricted]")
    out = json.loads(blob)
    return out["events"], out["result"]


def _event(e) -> dict:
    return {"node": e[0], "status": e[1], "latency_ms": e[2], "tokens_in": e[3], "tokens_out": e[4],
            "cost_inr": float(e[5]) if e[5] is not None else None, "detail": e[6], "at": e[7]}


def _totals(ev) -> dict:
    return {"total_cost_inr": round(sum(float(e[5] or 0) for e in ev), 4),
            "total_tokens": sum((e[3] or 0) + (e[4] or 0) for e in ev)}


# --- drafts, cases, approvals --------------------------------------------------------

@app.post("/drafts/{draft_id}/submit")
def submit(draft_id: str, user: User = Depends(current_user)) -> dict:
    require(user, "agent")
    try:
        out = cases.submit_draft(draft_id, submitted_by=user)
        flush_traces()
        return out
    except cases.NotFound:
        raise HTTPException(404, "draft not found")
    except cases.Forbidden as f:
        raise HTTPException(403, str(f))


@app.get("/cases/{case_id}")
def get_case(case_id: str, user: User = Depends(current_user)) -> dict:
    try:
        c = cases.get_case(case_id)
    except cases.NotFound:
        raise HTTPException(404, "case not found")
    if user.role != "supervisor" and c.get("submitted_by") != user.user_id:
        raise HTTPException(404, "case not found")
    with connect() as conn:
        ev = conn.execute("""SELECT node, status, latency_ms, tokens_in, tokens_out, cost_inr, detail, created_at
                             FROM run_events WHERE run_id = %s ORDER BY event_id""", (case_id,)).fetchall()
        c["evidence"] = redact_chunk_refs(conn, principal_for(user), c.get("evidence") or [])
    return {**c, "trace_url": trace_url(case_id), "events": [_event(e) for e in ev], **_totals(ev)}


@app.get("/approvals")
def list_approvals(state: Literal["pending"] = "pending", user: User = Depends(current_user)) -> list[dict]:
    require(user, "supervisor")
    rows = cases.pending_approvals()
    with connect() as conn:   # evidence from documents this supervisor can't read is redacted (D54)
        viewer = principal_for(user)
        for r in rows:
            r["evidence"] = redact_chunk_refs(conn, viewer, r.get("evidence") or [])
    return rows


class DecisionIn(BaseModel):
    # No approver_id: the approver is the signed-in supervisor (D50). A body that still sends
    # one is rejected rather than silently ignored, so an old client can't believe it chose.
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=500)
    # What the supervisor's screen showed (D55). Optional for scripts; the console sends all
    # three, and a mismatch with the current case is a 409 instead of approving something else.
    expected_txn_id: str | None = Field(default=None, max_length=64)
    expected_amount_inr: int | None = Field(default=None, gt=0)
    expected_draft_version: int | None = Field(default=None, ge=1)


@app.post("/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionIn, user: User = Depends(current_user)):
    require(user, "supervisor")
    if body.decision == "reject" and not body.reason:
        raise HTTPException(422, "a rejection needs a reason")
    try:
        out = cases.decide(approval_id, body.decision, None, body.reason, decided_by=user,
                           expected={"txn_id": body.expected_txn_id, "amount_inr": body.expected_amount_inr,
                                     "draft_version": body.expected_draft_version})
        flush_traces()
        return out
    except cases.NotFound:
        raise HTTPException(404, "approval not found")
    except cases.Forbidden as f:
        raise HTTPException(403, str(f))
    except cases.Conflict as c:
        return JSONResponse({"detail": "approval already decided", "existing": c.existing}, status_code=409)
    except cases.Stale as st:
        return JSONResponse({"detail": "case changed since you opened it; reload the queue",
                             "expected": st.expected, "actual": st.actual}, status_code=409)


# --- async jobs ------------------------------------------------------------------------

class JobIn(BaseModel):
    job_type: Literal["enron_compliance_scan", "tat_sweep"]
    payload: dict = Field(default_factory=dict)


@app.post("/jobs", status_code=202)
def create_job(body: JobIn, user: User = Depends(current_user)) -> dict:
    """Returns immediately with a job id; the worker container runs it."""
    from kestrel.jobs import queue
    require(user, "compliance", "supervisor")
    if body.job_type == "enron_compliance_scan" and user.role != "compliance":
        raise HTTPException(403, "only the compliance role can start a compliance scan")
    allowed = {"enron_compliance_scan": {"limit"}, "tat_sweep": {"as_of"}}[body.job_type]
    if set(body.payload) - allowed:   # e.g. no file paths from the client
        raise HTTPException(422, f"{body.job_type} accepts only {sorted(allowed)}")
    out = queue.enqueue(body.job_type, body.payload, requested_by=user.user_id)
    with connect() as conn:
        audit(conn, user, "job_created", "job", out.get("job_id"), {"job_type": body.job_type, "payload": body.payload})
        conn.commit()
    return out


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user)) -> dict:
    from kestrel.jobs import queue
    require(user, "compliance", "supervisor")
    try:
        return queue.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


# --- audit -------------------------------------------------------------------------------

@app.get("/audit")
def audit_trail(object_id: str | None = None, limit: int = 100, user: User = Depends(current_user)) -> list[dict]:
    """Read the append-only audit log, newest first (supervisors and compliance)."""
    require(user, "supervisor", "compliance")
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            """SELECT audit_id, at, actor_user_id, actor_role, actor_person_id, action, object_type, object_id, detail
               FROM audit_log WHERE %s::text IS NULL OR object_id = %s OR detail->>'case_id' = %s
               ORDER BY audit_id DESC LIMIT %s""", (object_id, object_id, object_id, limit)).fetchall()
    return [{"audit_id": r[0], "at": r[1], "actor": r[2], "actor_role": r[3], "actor_person_id": r[4],
             "action": r[5], "object_type": r[6], "object_id": r[7], "detail": r[8]} for r in rows]
