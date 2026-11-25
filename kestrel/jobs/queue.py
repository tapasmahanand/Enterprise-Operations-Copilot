"""Postgres job queue (no Redis): jobs table + SELECT … FOR UPDATE SKIP LOCKED.

Delivery is at-least-once: a worker that dies mid-job leaves `locked_at` behind, and
after LEASE the job is claimable again. Handlers are therefore written to be idempotent
(D31). Failed attempts back off exponentially up to max_attempts."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from kestrel.db import connect

LEASE = timedelta(minutes=5)
JOB_TYPES = {"enron_compliance_scan", "tat_sweep", "memory_summary"}


def enqueue(job_type: str, payload: dict[str, Any] | None = None, *, requested_by: str = "system",
            dedupe_key: str | None = None, max_attempts: int = 3) -> dict[str, Any]:
    """Returns the new job, or the live job with the same dedupe_key (no duplicate)."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    for _ in range(3):
        job_id = f"JOB-{uuid.uuid4().hex[:10]}"
        with connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO jobs (job_id, job_type, payload, requested_by, dedupe_key, max_attempts)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (job_id, job_type, Jsonb(payload or {}), requested_by, dedupe_key, max_attempts))
                conn.commit()
                return get_job(job_id)
            except UniqueViolation:
                conn.rollback()
                live = conn.execute("SELECT job_id FROM jobs WHERE dedupe_key = %s AND state IN ('queued','running')",
                                    (dedupe_key,)).fetchone()
        if live:
            return get_job(live[0])
        # The conflicting job finished between our INSERT and SELECT: try the insert again.
    raise RuntimeError(f"could not enqueue {job_type} with dedupe_key {dedupe_key!r}")


def claim(worker_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """UPDATE jobs SET state = 'running', attempts = attempts + 1, locked_at = now(), updated_at = now()
               WHERE job_id = (
                   SELECT job_id FROM jobs
                   WHERE (state = 'queued' AND run_after <= now())
                      OR (state = 'running' AND locked_at < now() - %s)
                   ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING job_id, job_type, payload, attempts, max_attempts""", (LEASE,)).fetchone()
        conn.commit()
    if row is None:
        return None
    return {"job_id": row[0], "job_type": row[1], "payload": row[2], "attempts": row[3], "max_attempts": row[4],
            "worker_id": worker_id}


# Every write after claim is fenced on (state='running', attempts=<this claim's attempt>).
# If this worker overran LEASE and another worker re-claimed the job, attempts moved on
# and the stale worker's progress/complete/fail touch nothing (returns False / "stale").
_FENCE = "job_id = %s AND state = 'running' AND attempts = %s"


def progress(job: dict[str, Any], data: dict[str, Any]) -> bool:
    """Also renews the lease, so a long job isn't stolen while it is making progress."""
    with connect() as conn:
        n = conn.execute(f"UPDATE jobs SET progress = %s, locked_at = now(), updated_at = now() WHERE {_FENCE}",
                         (Jsonb(data), job["job_id"], job["attempts"])).rowcount
        conn.commit()
    return n == 1


def complete(job: dict[str, Any], result: dict[str, Any]) -> bool:
    with connect() as conn:
        n = conn.execute("UPDATE jobs SET state = 'done', result = %s, error = NULL, locked_at = NULL, updated_at = now() "
                         f"WHERE {_FENCE}", (Jsonb(result), job["job_id"], job["attempts"])).rowcount
        conn.commit()
    return n == 1


def fail(job: dict[str, Any], error: str) -> str:
    """Retry with backoff (10 s, 40 s, …) or mark failed after max_attempts."""
    retry = job["attempts"] < job["max_attempts"]
    with connect() as conn:
        n = conn.execute(
            f"""UPDATE jobs SET state = %s, error = %s, locked_at = NULL, updated_at = now(),
                       run_after = now() + %s WHERE {_FENCE}""",
            ("queued" if retry else "failed", error[:2000], timedelta(seconds=10 * 4 ** (job["attempts"] - 1)),
             job["job_id"], job["attempts"])).rowcount
        conn.commit()
    if n == 0:
        return "stale"
    return "retrying" if retry else "failed"


def get_job(job_id: str) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute(
            """SELECT job_id, job_type, state, attempts, max_attempts, payload, progress, result, error,
                      requested_by, created_at, updated_at, run_after FROM jobs WHERE job_id = %s""", (job_id,)).fetchone()
    if r is None:
        raise KeyError(job_id)
    keys = ["job_id", "job_type", "state", "attempts", "max_attempts", "payload", "progress", "result", "error",
            "requested_by", "created_at", "updated_at", "run_after"]
    out = dict(zip(keys, r))
    for k in ("created_at", "updated_at", "run_after"):
        out[k] = out[k].isoformat()
    return out
