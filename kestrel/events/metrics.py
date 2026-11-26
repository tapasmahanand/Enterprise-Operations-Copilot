"""Event pipeline health, from Postgres alone (D56): how far behind the outbox is, and how
long an approval takes to become a posted credit."""

from __future__ import annotations

from typing import Any

from kestrel.db import connect


def snapshot(window_hours: int = 24) -> dict[str, Any]:
    with connect() as conn:
        unpublished, oldest = conn.execute(
            "SELECT count(*), extract(epoch FROM now() - min(created_at)) FROM outbox WHERE published_at IS NULL"
        ).fetchone()
        n, p50, p95, mx = conn.execute(
            """SELECT count(*),
                      percentile_cont(0.5)  WITHIN GROUP (ORDER BY s),
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY s), max(s)
               FROM (SELECT extract(epoch FROM r.recorded_at - a.decided_at) AS s
                     FROM ledger_receipts r JOIN approvals a ON r.idempotency_key = 'credit:' || a.approval_id
                     WHERE r.recorded_at > now() - make_interval(hours => %s)) t""", (window_hours,)).fetchone()
        stalled = conn.execute(
            """SELECT count(*) FROM approvals a JOIN cases c USING (case_id)
               WHERE a.state <> 'pending' AND c.status IN ('pending_approval', 'approved')
                 AND a.decided_at < now() - interval '2 minutes'""").fetchone()[0]
    r = lambda x: round(float(x), 3) if x is not None else None   # noqa: E731
    return {"outbox_unpublished": unpublished, "oldest_unpublished_age_s": r(oldest),
            "approval_to_post_s": {"n": n, "p50": r(p50), "p95": r(p95), "max": r(mx), "window_h": window_hours},
            "decided_cases_not_finished_after_2_min": stalled}
