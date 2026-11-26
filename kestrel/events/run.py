"""Event service (D56): outbox relay to NATS JetStream, durable consumers, reconciliation.

    NATS_URL=nats://localhost:4222 python -m kestrel.events.run

* **Relay.** Leases unpublished outbox rows (oldest first) and publishes each to JetStream
  with `Nats-Msg-Id = event_id`, so a re-publish inside the stream's duplicate window is
  dropped by the broker. A row is marked published only after the broker's PubAck. While
  the broker is down, rows stay in Postgres and are retried every second; nothing is lost
  because nothing was ever only in memory.
* **case-executor** (durable pull consumer, `kestrel.events.approval.*`): resumes the case's
  LangGraph workflow. Acks only after the handler returns, i.e. after the credit transaction
  and the graph checkpoint are committed. Busy (another executor holds the case) -> nak with
  a short delay; failure -> nak with backoff, then term after MAX_DELIVER.
* **notifier** (durable pull consumer, `kestrel.events.credit.posted`): drafts the customer
  SMS and closes the case.
* **Reconcile** (every RECONCILE_INTERVAL_S): a decision whose event was published but whose
  case has not finished after STALL_AFTER_S is executed directly. It covers a consumer that
  lost a message, not a broker outage (the relay waits for the broker).

Both consumers are idempotent (ledger receipt, graph checkpoint, notification unique per
case), so JetStream's at-least-once redelivery cannot post a second credit or SMS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any, Callable

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, StreamConfig

from kestrel import outbox
from kestrel.db import connect

log = logging.getLogger("events")

STREAM = os.environ.get("NATS_STREAM", "KESTREL")
SUBJECTS = {"ApprovalGranted": "kestrel.events.approval.granted",
            "ApprovalRejected": "kestrel.events.approval.rejected",
            "CreditPosted": "kestrel.events.credit.posted"}
MAX_DELIVER = int(os.environ.get("EVENTS_MAX_DELIVER", "10"))
ACK_WAIT_S = float(os.environ.get("EVENTS_ACK_WAIT_S", "120"))   # redelivery after a consumer dies mid-message
STALL_AFTER_S = float(os.environ.get("STALL_AFTER_S", "120"))
RECONCILE_INTERVAL_S = float(os.environ.get("RECONCILE_INTERVAL_S", "30"))
METRICS_INTERVAL_S = float(os.environ.get("METRICS_INTERVAL_S", "30"))


async def connect_nats(url: str) -> Any:
    """Keep trying until the broker answers; once connected the client reconnects by itself."""
    while True:
        try:
            return await nats.connect(url, max_reconnect_attempts=-1, reconnect_time_wait=1, connect_timeout=2,
                                      name="kestrel-events")
        except Exception as e:  # noqa: BLE001
            log.warning("NATS not reachable at %s (%r); retrying", url, e)
            await asyncio.sleep(1)


async def ensure_stream(js: Any) -> None:
    cfg = StreamConfig(name=STREAM, subjects=["kestrel.events.>"], duplicate_window=120.0)
    try:
        await js.add_stream(cfg)
    except Exception:  # noqa: BLE001 - exists already (or broker briefly away): update is idempotent
        try:
            await js.update_stream(cfg)
        except Exception:  # noqa: BLE001
            log.debug("stream ensure deferred")


# --- relay ----------------------------------------------------------------------------------

async def relay(js: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        evs = await asyncio.to_thread(outbox.claim, 50, None, 30)
        if not evs:
            await asyncio.sleep(0.3)
            continue
        for i, ev in enumerate(evs):
            subject = SUBJECTS.get(ev["event_type"])
            if subject is None:
                await asyncio.to_thread(outbox.mark_failed, ev["event_id"], ev["attempts"], "no subject for type")
                continue
            try:
                await js.publish(subject, json.dumps(ev["payload"], default=str).encode(),
                                 headers={"Nats-Msg-Id": ev["event_id"]}, timeout=2)
            except Exception as e:  # noqa: BLE001 - broker away: give the whole rest of the batch back
                log.warning("publish failed (%r); %d events wait in the outbox", e, len(evs) - i)
                for rest in evs[i:]:
                    await asyncio.to_thread(outbox.mark_failed, rest["event_id"], rest["attempts"],
                                            f"publish: {type(e).__name__}", 1.0)
                await asyncio.sleep(1)
                break
            await asyncio.to_thread(outbox.mark_published, ev["event_id"])


# --- consumers --------------------------------------------------------------------------------

async def consume(js: Any, durable: str, subject: str, handler: Callable[[dict], Any], stop: asyncio.Event) -> None:
    cfg = ConsumerConfig(durable_name=durable, ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL,
                         ack_wait=ACK_WAIT_S, max_deliver=MAX_DELIVER, filter_subject=subject)
    while not stop.is_set():
        try:
            await ensure_stream(js)
            sub = await js.pull_subscribe(subject, durable=durable, stream=STREAM, config=cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: subscribe failed (%r); retrying", durable, e)
            await asyncio.sleep(1)
            continue
        while not stop.is_set():
            try:
                msgs = await sub.fetch(10, timeout=1)
            except NatsTimeout:
                continue
            except Exception as e:  # noqa: BLE001 - broker restart: resubscribe
                log.warning("%s: fetch failed (%r); resubscribing", durable, e)
                await asyncio.sleep(1)
                break
            for m in msgs:
                await _handle(durable, m, handler)


async def _handle(durable: str, m: Any, handler: Callable[[dict], Any]) -> None:
    payload = json.loads(m.data)
    n = m.metadata.num_delivered if m.metadata else 1
    try:
        await asyncio.to_thread(handler, payload)
    except outbox.Busy:
        await m.nak(delay=2)
        return
    except Exception as e:  # noqa: BLE001
        if n >= MAX_DELIVER:
            log.error("%s: giving up on %s after %d deliveries: %r (reconcile will retry)",
                      durable, payload.get("event_id"), n, e)
            await m.term()
        else:
            log.warning("%s: %s failed (delivery %d): %r", durable, payload.get("event_id"), n, e)
            await m.nak(delay=min(30, 2 ** n))
        return
    except BaseException as e:   # SimulatedCrash or similar: die like a killed process, unacked
        if isinstance(e, asyncio.CancelledError):
            raise
        log.critical("%s: process-ending error in %s: %r", durable, payload.get("event_id"), e)
        os._exit(70)
    await m.ack()   # only after the handler's writes are committed


# --- reconcile --------------------------------------------------------------------------------

def reconcile_stalled(stall_after_s: float = STALL_AFTER_S) -> dict[str, int]:
    """Cases decided more than `stall_after_s` ago whose events were published but which have
    not finished: run the handlers directly. Idempotent, like the consumers."""
    from kestrel.graphs.case import handle_decision_event, notify_credit_posted
    with connect() as conn:
        rows = conn.execute(
            """SELECT o.event_type, o.payload FROM outbox o JOIN cases c ON c.case_id = o.aggregate_id
               WHERE o.published_at IS NOT NULL AND o.published_at < now() - make_interval(secs => %s)
                 AND ((o.event_type IN ('ApprovalGranted', 'ApprovalRejected') AND c.status = 'pending_approval')
                   OR (o.event_type = 'CreditPosted' AND c.status = 'approved'))
               ORDER BY o.created_at LIMIT 50""", (stall_after_s,)).fetchall()
    done = 0
    for etype, payload in rows:
        try:
            (notify_credit_posted if etype == "CreditPosted" else handle_decision_event)(payload)
            done += 1
        except outbox.Busy:
            pass
        except Exception:  # noqa: BLE001
            log.exception("reconcile of %s failed", payload.get("event_id"))
    if done:
        log.info("reconcile finished %d stalled events", done)
    return {"reconciled": done}


async def periodic(fn: Callable[[], Any], every_s: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every_s)
        except asyncio.TimeoutError:
            pass
        if not stop.is_set():
            try:
                await asyncio.to_thread(fn)
            except Exception:  # noqa: BLE001
                log.exception("periodic %s failed", getattr(fn, "__name__", fn))


def _log_metrics() -> None:
    from kestrel.events.metrics import snapshot
    log.info("metrics %s", json.dumps(snapshot()))


async def main_async() -> None:
    from kestrel.graphs.case import handle_decision_event, notify_credit_posted
    url = os.environ.get("NATS_URL", "nats://localhost:4222")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    nc = await connect_nats(url)
    js = nc.jetstream()
    await ensure_stream(js)
    log.info("events service up: %s, stream %s", url, STREAM)
    tasks = [asyncio.create_task(relay(js, stop)),
             asyncio.create_task(consume(js, "case-executor", "kestrel.events.approval.*", handle_decision_event, stop)),
             asyncio.create_task(consume(js, "notifier", "kestrel.events.credit.posted", notify_credit_posted, stop)),
             asyncio.create_task(periodic(reconcile_stalled, RECONCILE_INTERVAL_S, stop)),
             asyncio.create_task(periodic(_log_metrics, METRICS_INTERVAL_S, stop))]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await nc.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
