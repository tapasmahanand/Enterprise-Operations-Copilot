"""Transactional outbox (D55).

`enqueue(conn, ...)` inserts an event on the caller's connection, so it commits or rolls
back with the change it describes (an approval decision, a posted credit). Nothing is sent
anywhere inside that transaction.

`dispatch_due()` hands committed, unhandled events to their in-process handler. It is the
delivery path when no broker is configured, and the recovery path when one is (step 3 adds
NATS JetStream). Delivery is at least once: an event whose handler crashed is retried after
its lease runs out, so every handler is idempotent (the credit by its ledger receipt, the
graph by its checkpoint, the notification by case id).
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from kestrel.db import connect

log = logging.getLogger("outbox")

LEASE_S = 60
MAX_BACKOFF_S = 300


def event_id_for(approval_id: str, kind: str) -> str:
    """Deterministic ids: the same fact always gets the same id, so re-enqueueing it is a
    no-op and a broker's duplicate filter can drop a re-publish."""
    return f"EVT-{approval_id}-{kind}"


def enqueue(conn: psycopg.Connection, event_type: str, aggregate_id: str, payload: dict[str, Any],
            event_id: str) -> bool:
    """Insert on the caller's transaction. Returns False if the event already existed."""
    return conn.execute(
        """INSERT INTO outbox (event_id, event_type, aggregate_id, payload) VALUES (%s, %s, %s, %s)
           ON CONFLICT (event_id) DO NOTHING""",
        (event_id, event_type, aggregate_id, Jsonb({**payload, "event_id": event_id, "event_type": event_type}))
    ).rowcount == 1


def claim(limit: int = 20, event_id: str | None = None, lease_s: int = LEASE_S) -> list[dict[str, Any]]:
    """Lease up to `limit` unpublished events (oldest first) that nobody else holds."""
    with connect() as conn:
        rows = conn.execute(
            """UPDATE outbox SET claimed_until = now() + make_interval(secs => %(lease)s), attempts = attempts + 1
               WHERE event_id IN (
                   SELECT event_id FROM outbox
                   WHERE published_at IS NULL AND (claimed_until IS NULL OR claimed_until < now())
                     AND (%(id)s::text IS NULL OR event_id = %(id)s)
                   ORDER BY created_at LIMIT %(n)s FOR UPDATE SKIP LOCKED)
               RETURNING event_id, event_type, aggregate_id, payload, attempts""",
            {"lease": lease_s, "id": event_id, "n": limit}).fetchall()
        conn.commit()
    return [{"event_id": r[0], "event_type": r[1], "aggregate_id": r[2], "payload": r[3], "attempts": r[4]}
            for r in rows]


def mark_published(event_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE outbox SET published_at = now(), claimed_until = NULL, last_error = NULL"
                     " WHERE event_id = %s", (event_id,))
        conn.commit()


def mark_failed(event_id: str, attempts: int, error: str) -> None:
    """Keep it unpublished; push the lease out with exponential backoff."""
    delay = min(MAX_BACKOFF_S, 2 ** min(attempts, 8))
    with connect() as conn:
        conn.execute("""UPDATE outbox SET last_error = %s, claimed_until = now() + make_interval(secs => %s)
                        WHERE event_id = %s""", (error[:1000], delay, event_id))
        conn.commit()


class Busy(Exception):
    """Another worker is executing this case right now; retry later (not a failure)."""


Handler = Callable[[dict[str, Any]], Any]


def handlers() -> dict[str, Handler]:
    from kestrel.graphs.case import handle_decision_event
    return {"ApprovalGranted": handle_decision_event, "ApprovalRejected": handle_decision_event,
            # The case graph drafts the notification itself until step 3 moves it to a consumer.
            "CreditPosted": lambda payload: None}


def dispatch(ev: dict[str, Any]) -> bool:
    """Run one claimed event's handler. True if it was handled (and marked published)."""
    h = handlers().get(ev["event_type"])
    try:
        if h is None:
            raise KeyError(f"no handler for {ev['event_type']}")
        h(ev["payload"])
    except Busy:
        mark_failed(ev["event_id"], 0, "busy: case is being executed elsewhere")
        return False
    except Exception as e:  # noqa: BLE001 - one bad event must not stop the others
        log.warning("event %s failed: %r", ev["event_id"], e)
        mark_failed(ev["event_id"], ev["attempts"], f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        return False
    mark_published(ev["event_id"])
    return True


def dispatch_due(limit: int = 20, event_id: str | None = None) -> dict[str, int]:
    done = failed = 0
    for ev in claim(limit, event_id):
        if dispatch(ev):
            done += 1
        else:
            failed += 1
    return {"dispatched": done, "failed": failed}


def oldest_unpublished_age_s() -> float | None:
    with connect() as conn:
        r = conn.execute("SELECT extract(epoch FROM now() - min(created_at)) FROM outbox WHERE published_at IS NULL"
                         ).fetchone()
    return float(r[0]) if r and r[0] is not None else None
