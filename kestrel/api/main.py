"""FastAPI app. Milestone 1: health and readiness only."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from kestrel.config import get_settings
from kestrel.db import connect

app = FastAPI(title="Kestrel Resolution Copilot", version="0.1.0")


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
        checks["database"] = "ok"
        checks["knowledge_base"] = {"documents": docs, "chunks": chunks, "banking77_queries": queries}
        kb_ready = docs > 0 and chunks > 0
    except Exception as e:  # noqa: BLE001 - health must report, not raise
        checks["database"] = f"error: {type(e).__name__}"
        kb_ready = False
    checks["model_key"] = "configured" if s.openai_api_key else "missing"
    checks["embedding_provider"] = s.embedding_provider
    ready = checks["database"] == "ok" and kb_ready
    return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503)
