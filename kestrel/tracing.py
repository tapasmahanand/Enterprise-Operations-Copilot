"""Per-node tracing. Every node writes a run_events row (the fallback that never depends
on Langfuse); when Langfuse is configured the same data also goes there as spans."""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

from psycopg.types.json import Jsonb

from kestrel.db import connect

log = logging.getLogger("trace")


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


def traced(node: str, run_key: str = "run_id", thread_key: str = "thread_id") -> Callable:
    """Decorator for graph nodes: times the node and records a run_event with the
    `_trace` dict the node may put in its return value (removed before returning)."""
    def deco(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        @functools.wraps(fn)
        def wrapper(state: dict, *args, **kwargs) -> dict:
            t0 = time.perf_counter()
            try:
                out = fn(state, *args, **kwargs) or {}
            except Exception as e:
                if type(e).__name__ == "GraphInterrupt":
                    record_event(state.get(run_key, "?"), state.get(thread_key), node, "interrupted",
                                 int((time.perf_counter() - t0) * 1000))
                else:
                    record_event(state.get(run_key, "?"), state.get(thread_key), node, "error",
                                 int((time.perf_counter() - t0) * 1000), {"error": repr(e)[:500]})
                raise
            tr = out.pop("_trace", {}) if isinstance(out, dict) else {}
            record_event(state.get(run_key, "?"), state.get(thread_key), node, tr.pop("status", "ok"),
                         int((time.perf_counter() - t0) * 1000), tr.get("detail"), tr.get("tokens_in"),
                         tr.get("tokens_out"), tr.get("cost_inr"))
            return out
        return wrapper
    return deco
