"""Posting an approved credit exactly once, and only the credit that was approved (D55).

Two guarantees:

1. **Binding.** `check_binding` compares what the supervisor approved (frozen on the
   approvals row at decision time: transaction, amount, draft version, hash of the action)
   with the case and draft as they are now. Any difference stops the posting.

2. **One credit per approval.** `post_credit_and_advance` writes the credit, its ledger
   receipt (idempotency key `credit:<approval_id>`), the case's new status and a
   `CreditPosted` outbox event in **one** Postgres transaction. A replay after a crash finds
   the receipt and returns it; it never inserts a second credit.

For a real core-banking API the local transaction can't include the bank's write. The
pattern is then `post_remote_credit`: send the same idempotency key on every attempt and,
after an ambiguous failure (timeout, connection reset), ask the ledger whether that key was
applied before retrying. It is implemented against a client protocol and unit-tested with a
fake; there is no real bank API here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from kestrel import outbox
from kestrel.db import connect
from kestrel.tools.base import ToolError


def action_hash(action: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(action, sort_keys=True, default=str).encode()).hexdigest()


def binding_for(conn: psycopg.Connection, approval_id: str) -> dict[str, Any]:
    """What a decision binds to, read from the approval's own payload and its draft."""
    r = conn.execute("""SELECT a.action_payload, d.version FROM approvals a JOIN cases c USING (case_id)
                        LEFT JOIN drafts d ON d.draft_id = c.draft_id WHERE a.approval_id = %s""",
                     (approval_id,)).fetchone()
    action, version = r
    return {"txn_id": action.get("txn_id"), "amount_inr": action.get("amount_inr"),
            "draft_version": version, "action_hash": action_hash(action)}


def check_binding(conn: psycopg.Connection, approval_id: str) -> str | None:
    """None if the case still matches the approval; otherwise why not."""
    r = conn.execute(
        """SELECT a.state, a.bound_txn_id, a.bound_amount_inr, a.bound_draft_version, a.bound_action_hash,
                  c.recommended_action, d.action, d.version
           FROM approvals a JOIN cases c USING (case_id) LEFT JOIN drafts d ON d.draft_id = c.draft_id
           WHERE a.approval_id = %s""", (approval_id,)).fetchone()
    if r is None:
        return "approval not found"
    state, txn, amount, version, h, rec, draft_action, draft_version = r
    if state != "approved":
        return f"approval is {state}, not approved"
    if h is None:
        return "approval has no binding (decided before D55); re-decide it"
    case_action = {k: v for k, v in (rec or {}).items() if k != "calc"}
    if action_hash(case_action) != h:
        return "case action differs from what was approved"
    if draft_action is not None and (action_hash(draft_action) != h or draft_version != version):
        return f"draft changed since approval (version {draft_version}, approved {version})"
    if case_action.get("txn_id") != txn or case_action.get("amount_inr") != amount:
        return "transaction or amount differs from what was approved"
    return None


def post_credit_and_advance(*, case_id: str, approval_id: str, account_id: str, txn_id: str | None,
                            amount_inr: int, kind: str, reason: str, card_last4: str | None = None) -> dict[str, Any]:
    """Mock ledger: credit + receipt + case status + CreditPosted, all in one transaction.
    Returns the receipt; `reused_receipt` is True when this approval had already posted."""
    key = f"credit:{approval_id}"
    with connect() as conn:
        try:
            with conn.transaction():
                got = conn.execute("SELECT response FROM ledger_receipts WHERE idempotency_key = %s FOR UPDATE",
                                   (key,)).fetchone()
                if got:
                    return {**got[0], "reused_receipt": True}
                if txn_id:
                    other = conn.execute("SELECT credit_id, case_id FROM mock_credits WHERE txn_id = %s",
                                         (txn_id,)).fetchone()
                    if other:   # D38: one credit per transaction, whichever case asks
                        raise ToolError(f"transaction {txn_id} was already credited ({other[0]}, case {other[1]})")
                credit_id = f"CR-{uuid.uuid4().hex[:10]}"
                posted = conn.execute(
                    """INSERT INTO mock_credits (credit_id, case_id, account_id, txn_id, kind, amount_inr, reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING posted_at""",
                    (credit_id, case_id, account_id, txn_id, kind, amount_inr, reason)).fetchone()[0]
                receipt = {"credit_id": credit_id, "amount_inr": amount_inr, "kind": kind,
                           "posted_at": posted.isoformat(timespec="seconds"), "idempotency_key": key}
                conn.execute("INSERT INTO ledger_receipts (idempotency_key, case_id, credit_id, response)"
                             " VALUES (%s, %s, %s, %s)", (key, case_id, credit_id, Jsonb(receipt)))
                conn.execute("""UPDATE cases SET status = 'approved', result = COALESCE(result, '{}'::jsonb) || %s,
                                updated_at = now() WHERE case_id = %s""", (Jsonb({"credit": receipt}), case_id))
                outbox.enqueue(conn, "CreditPosted", case_id,
                               {"case_id": case_id, "approval_id": approval_id, "account_id": account_id,
                                "txn_id": txn_id, "card_last4": card_last4, **receipt},
                               event_id=outbox.event_id_for(approval_id, "credit-posted"))
        except psycopg.errors.UniqueViolation:
            # Lost a race with another executor for the same approval: its receipt is the answer.
            got = conn.execute("SELECT response FROM ledger_receipts WHERE idempotency_key = %s", (key,)).fetchone()
            if got is None:
                raise
            return {**got[0], "reused_receipt": True}
    return {**receipt, "reused_receipt": False}


# --- real banking API pattern (not wired: there is no real ledger here) ---------------------

class LedgerClient(Protocol):
    def post(self, *, idempotency_key: str, account_id: str, amount_inr: int, reference: str) -> dict: ...
    def find(self, idempotency_key: str) -> dict | None: ...


class AmbiguousLedgerResult(Exception):
    pass


def post_remote_credit(client: LedgerClient, *, idempotency_key: str, account_id: str, amount_inr: int,
                       reference: str, attempts: int = 3) -> dict:
    """Same key on every attempt; after a timeout, look the key up before trying again, because
    the bank may have applied the first request and only the response was lost."""
    for _ in range(attempts):
        try:
            return client.post(idempotency_key=idempotency_key, account_id=account_id,
                               amount_inr=amount_inr, reference=reference)
        except (TimeoutError, ConnectionError):
            found = client.find(idempotency_key)
            if found is not None:
                return found
    raise AmbiguousLedgerResult(f"{idempotency_key}: no confirmation after {attempts} attempts; "
                                "leave the case for reconciliation, do not post again by hand")
