"""Worker process: claims jobs from Postgres and runs them; also schedules the TAT sweep.

    python -m kestrel.jobs.worker            # runs forever (compose service `worker`)
    python -m kestrel.jobs.worker --once     # drain the queue and exit (tests)
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time
import traceback

from kestrel.jobs import enron_scan, memory_summary, queue, tat_sweep
from kestrel.tracing import record_event, root_span

log = logging.getLogger("worker")
HANDLERS = {"enron_compliance_scan": enron_scan.run, "tat_sweep": tat_sweep.run, "memory_summary": memory_summary.run}


def run_one(worker_id: str) -> bool:
    job = queue.claim(worker_id)
    if job is None:
        return False
    jid, t0 = job["job_id"], time.perf_counter()
    log.info("claimed %s (%s) attempt %d", jid, job["job_type"], job["attempts"])
    try:
        with root_span(jid, f"job {job['job_type']}", attempt=job["attempts"]):
            result = HANDLERS[job["job_type"]](job["payload"], lambda p: queue.progress(jid, p))
        queue.complete(jid, result)
        record_event(jid, jid, job["job_type"], "ok", int((time.perf_counter() - t0) * 1000),
                     {"attempt": job["attempts"], "summary": {k: v for k, v in result.items() if k != "sample"}})
    except Exception as e:  # noqa: BLE001 - a job failure must not kill the worker
        outcome = queue.fail(job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        record_event(jid, jid, job["job_type"], "error", int((time.perf_counter() - t0) * 1000),
                     {"attempt": job["attempts"], "error": repr(e)[:300], "outcome": outcome})
        log.warning("job %s failed (%s): %r", jid, outcome, e)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    sweep_every = float(os.environ.get("TAT_SWEEP_INTERVAL_S", "300"))
    next_sweep = 0.0
    log.info("worker %s up; TAT sweep every %.0f s", worker_id, sweep_every)
    while True:
        if not a.once and time.time() >= next_sweep:
            queue.enqueue("tat_sweep", {}, requested_by="scheduler", dedupe_key="tat_sweep:scheduled")
            next_sweep = time.time() + sweep_every
        busy = run_one(worker_id)
        if a.once and not busy:
            return
        if not busy:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
