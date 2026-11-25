"""Per-node tracing. Every node writes a run_events row (the fallback that never depends
on Langfuse); when LANGFUSE_* keys are set the same node also becomes a Langfuse
observation. The Langfuse trace id is derived from the run id (chat) or case id, so a
case's spans before and after a restart land in the same trace without any in-memory
context."""

from __future__ import annotations

import functools
import logging
import os
import time
from functools import lru_cache
from typing import Any, Callable

from psycopg.types.json import Jsonb

from kestrel.db import connect

log = logging.getLogger("trace")

# Langfuse observation type per node: lets the Langfuse UI show guardrails, tools,
# retrieval and LLM generations distinctly.
OBS_TYPE = {"compliance_in": "guardrail", "compliance_out": "guardrail", "blocked": "guardrail",
            "supervisor": "generation", "composer": "generation", "retrieval": "retriever",
            "tool_exec": "tool", "execute_action": "tool", "notify": "tool", "approval_gate": "span"}


@lru_cache(maxsize=1)
def langfuse():
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse
        return Langfuse()
    except Exception:  # noqa: BLE001
        log.exception("Langfuse init failed; run_events only")
        return None


def trace_id_for(run_id: str) -> str | None:
    lf = langfuse()
    return lf.create_trace_id(seed=run_id) if lf else None


def trace_url(run_id: str) -> str | None:
    lf, tid = langfuse(), trace_id_for(run_id)
    if not (lf and tid):
        return None
    try:
        url = lf.get_trace_url(trace_id=tid)
        public = os.environ.get("LANGFUSE_PUBLIC_URL")   # host-facing URL when the app sees an internal one
        return url.replace(os.environ.get("LANGFUSE_HOST", ""), public) if (url and public) else url
    except Exception:  # noqa: BLE001
        return None


def record_event(run_id: str, thread_id: str | None, node: str, status: str, latency_ms: int,
                 detail: dict[str, Any] | None = None, tokens_in: int | None = None,
                 tokens_out: int | None = None, cost_inr: float | None = None) -> None:
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO run_events (run_id, thread_id, node, status, latency_ms, tokens_in, tokens_out, cost_inr, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, thread_id, node, status, latency_ms, tokens_in, tokens_out, cost_inr, Jsonb(detail or {})))
            conn.commit()
    except Exception:  # noqa: BLE001 - tracing must never break the request
        log.exception("failed to record run_event for %s", node)


_ROOTS: dict[str, Any] = {}
_TRACE_ATTRS: dict[str, dict[str, Any]] = {}


def _tag(obs, run_id: str) -> None:
    """Stamp trace-level attributes (name, session) on a span. Set on every span of the
    run, not only the root, because node spans are created on LangGraph worker threads
    where OpenTelemetry context propagation can't be relied on."""
    attrs = _TRACE_ATTRS.get(run_id) or {}
    try:
        for k, v in attrs.items():
            obs._otel_span.set_attribute(k, v)
    except Exception:  # noqa: BLE001
        pass


class root_span:
    """Context manager: one parent observation per request (a chat run, a case submit,
    a case decision), so the Langfuse trace is named and nested. Node spans attach to it
    by run id. In-process only; after a restart the next request opens a new root in
    the same trace (same deterministic trace id)."""

    def __init__(self, run_id: str, name: str, **metadata: Any):
        self.run_id, self.name, self.metadata, self.obs = run_id, name, metadata, None

    def __enter__(self):
        lf = langfuse()
        if lf:
            from langfuse import LangfuseOtelSpanAttributes as A
            attrs = {A.TRACE_NAME: "chat run" if self.name == "chat_run" else f"case {self.run_id}"}
            if sid := self.metadata.get("session_id"):
                attrs[A.TRACE_SESSION_ID] = sid
            _TRACE_ATTRS[self.run_id] = attrs
            try:
                self.obs = lf.start_observation(trace_context={"trace_id": lf.create_trace_id(seed=self.run_id)},
                                                name=self.name, as_type="chain", metadata=self.metadata)
                _ROOTS[self.run_id] = self.obs
                _tag(self.obs, self.run_id)
            except Exception:  # noqa: BLE001
                log.exception("langfuse root failed")
        return self

    def __exit__(self, *exc):
        _ROOTS.pop(self.run_id, None)
        _TRACE_ATTRS.pop(self.run_id, None)
        if self.obs is not None:
            try:
                self.obs.end()
            except Exception:  # noqa: BLE001
                pass
        flush()
        return False


def _start_obs(run_id: str, node: str, state: dict):
    lf = langfuse()
    if not lf:
        return None
    try:
        ctx: dict[str, str] = {"trace_id": lf.create_trace_id(seed=run_id)}
        if (root := _ROOTS.get(run_id)) is not None:
            ctx["parent_span_id"] = root.id
        obs = lf.start_observation(
            trace_context=ctx, name=node,
            as_type=OBS_TYPE.get(node, "span"),
            input={k: state.get(k) for k in ("masked", "sup", "txn", "action", "decision") if state.get(k)},
            metadata={"run_id": run_id, "thread_id": state.get("thread_id")})
        _tag(obs, run_id)
        return obs
    except Exception:  # noqa: BLE001
        log.exception("langfuse start failed")
        return None


def _end_obs(obs, status: str, tr: dict) -> None:
    if obs is None:
        return
    try:
        upd: dict[str, Any] = {"output": tr.get("detail"),
                               "level": {"error": "ERROR", "blocked": "WARNING"}.get(status, "DEFAULT")}
        if tr.get("tokens_in") is not None:
            upd["usage_details"] = {"input": tr["tokens_in"], "output": tr.get("tokens_out") or 0}
            upd["model"] = (tr.get("detail") or {}).get("model")
            if tr.get("cost_inr") is not None:
                upd["cost_details"] = {"total": tr["cost_inr"] / float(os.environ.get("USD_INR", "88"))}  # Langfuse wants USD
        obs.update(**upd)
        obs.end()
    except Exception:  # noqa: BLE001
        log.exception("langfuse end failed")


def traced(node: str, run_key: str = "run_id", thread_key: str = "thread_id") -> Callable:
    """Decorator for graph nodes: times the node and records a run_event (and a Langfuse
    observation) with the `_trace` dict the node may put in its return value."""
    def deco(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        @functools.wraps(fn)
        def wrapper(state: dict, *args, **kwargs) -> dict:
            run_id = state.get(run_key, "?")
            obs = _start_obs(run_id, node, state)
            t0 = time.perf_counter()
            try:
                out = fn(state, *args, **kwargs) or {}
            except Exception as e:
                ms = int((time.perf_counter() - t0) * 1000)
                if type(e).__name__ == "GraphInterrupt":
                    record_event(run_id, state.get(thread_key), node, "interrupted", ms, {"waiting_for": "supervisor"})
                    _end_obs(obs, "interrupted", {"detail": {"waiting_for": "supervisor decision"}})
                else:
                    record_event(run_id, state.get(thread_key), node, "error", ms, {"error": repr(e)[:500]})
                    _end_obs(obs, "error", {"detail": {"error": repr(e)[:500]}})
                raise
            tr = out.pop("_trace", {}) if isinstance(out, dict) else {}
            status = tr.pop("status", "ok")
            record_event(run_id, state.get(thread_key), node, status, int((time.perf_counter() - t0) * 1000),
                         tr.get("detail"), tr.get("tokens_in"), tr.get("tokens_out"), tr.get("cost_inr"))
            _end_obs(obs, status, tr)
            return out
        return wrapper
    return deco


def flush() -> None:
    lf = langfuse()
    if lf:
        try:
            lf.flush()
        except Exception:  # noqa: BLE001
            pass
