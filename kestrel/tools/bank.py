"""Mock bank systems. Every tool is real DB I/O against the seeded mock tables, so it has
real failure modes (not found, invalid state). Write tools are idempotent by key."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from kestrel.db import connect
from kestrel.rules.calculator import LiabilityInput, assess_liability
from kestrel.rules.loader import load_rules
from kestrel.tools.base import ToolError, ToolResult, run_tool


# --- read tools -----------------------------------------------------------------

class TxnLookupArgs(BaseModel):
    account_id: str | None = None
    card_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    amount_inr: int | None = None
    days_back: int = Field(default=30, ge=1, le=180)
    as_of: date | None = None


def _txn_lookup(a: TxnLookupArgs) -> list[dict]:
    if not a.account_id and not a.card_last4:
        raise ToolError("need account_id or card_last4")
    as_of = a.as_of or date.today()
    where, params = ["t.txn_date BETWEEN %(lo)s AND %(hi)s"], {"lo": as_of - timedelta(days=a.days_back), "hi": as_of}
    if a.account_id:
        where.append("t.account_id = %(acc)s"); params["acc"] = a.account_id
    if a.card_last4:
        where.append("c.last4 = %(l4)s"); params["l4"] = a.card_last4
    if a.amount_inr:
        where.append("t.amount_inr = %(amt)s"); params["amt"] = a.amount_inr
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT t.txn_id, t.account_id, t.card_id, c.last4, t.txn_date, t.alert_sent_on, t.channel,
                   t.merchant, t.amount_inr, t.status, t.cross_border, a.product, a.credit_limit_inr, a.customer_id
            FROM mock_transactions t JOIN mock_accounts a USING (account_id)
            LEFT JOIN mock_cards c ON c.card_id = t.card_id
            WHERE {' AND '.join(where)} ORDER BY t.txn_date DESC""", params).fetchall()
    cols = ["txn_id", "account_id", "card_id", "card_last4", "txn_date", "alert_sent_on", "channel", "merchant",
            "amount_inr", "status", "cross_border", "product", "credit_limit_inr", "customer_id"]
    return [dict(zip(cols, r)) for r in rows]


def transaction_lookup(**kw) -> ToolResult:
    return run_tool(_txn_lookup, TxnLookupArgs, kw)


class TicketLookupArgs(BaseModel):
    ticket_id: str | None = None
    account_id: str | None = None


def _ticket_lookup(a: TicketLookupArgs) -> list[dict]:
    if not a.ticket_id and not a.account_id:
        raise ToolError("need ticket_id or account_id")
    with connect() as conn:
        rows = conn.execute(
            """SELECT ticket_id, account_id, opened_on, category, description, status FROM mock_tickets
               WHERE (%(t)s::text IS NULL OR ticket_id = %(t)s) AND (%(a)s::text IS NULL OR account_id = %(a)s)""",
            {"t": a.ticket_id, "a": a.account_id}).fetchall()
    return [dict(zip(["ticket_id", "account_id", "opened_on", "category", "description", "status"], r)) for r in rows]


def ticket_lookup(**kw) -> ToolResult:
    return run_tool(_ticket_lookup, TicketLookupArgs, kw)


class CalcArgs(LiabilityInput):
    pass


def _calc(a: CalcArgs) -> dict:
    return assess_liability(a, load_rules()).model_dump(mode="json")


def liability_calculator(**kw) -> ToolResult:
    return run_tool(_calc, CalcArgs, kw, retries=0)


# --- write tools ----------------------------------------------------------------

class BlockCardArgs(BaseModel):
    card_id: str
    reason: str = Field(min_length=3, max_length=200)


def _block_card(a: BlockCardArgs) -> dict:
    """Idempotent: blocking an already-blocked card succeeds and says so."""
    with connect() as conn:
        row = conn.execute("SELECT status, last4 FROM mock_cards WHERE card_id = %s FOR UPDATE", (a.card_id,)).fetchone()
        if row is None:
            raise ToolError(f"card {a.card_id} not found")
        already = row[0] == "blocked"
        if not already:
            conn.execute("UPDATE mock_cards SET status = 'blocked' WHERE card_id = %s", (a.card_id,))
        conn.commit()
    return {"card_id": a.card_id, "last4": row[1], "status": "blocked", "already_blocked": already,
            "blocked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def block_card(**kw) -> ToolResult:
    return run_tool(_block_card, BlockCardArgs, kw)


class SubmitCreditArgs(BaseModel):
    case_id: str
    account_id: str
    txn_id: str | None = None
    amount_inr: int = Field(gt=0)
    kind: Literal["refund", "shadow_credit"]
    reason: str


def _submit_credit(a: SubmitCreditArgs) -> dict:
    """Idempotent by case_id: a resumed graph that re-runs this step gets the original credit back."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO mock_credits (credit_id, case_id, account_id, txn_id, kind, amount_inr, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (case_id) DO NOTHING""",
            (f"CR-{uuid.uuid4().hex[:10]}", a.case_id, a.account_id, a.txn_id, a.kind, a.amount_inr, a.reason))
        row = conn.execute("SELECT credit_id, amount_inr, kind, posted_at FROM mock_credits WHERE case_id = %s",
                           (a.case_id,)).fetchone()
        conn.commit()
    return {"credit_id": row[0], "amount_inr": row[1], "kind": row[2], "posted_at": row[3].isoformat(timespec="seconds")}


def submit_credit(**kw) -> ToolResult:
    return run_tool(_submit_credit, SubmitCreditArgs, kw)


TEMPLATES = {
    "credit_approved": "Dear customer, ₹{amount} has been credited to your Kestrel account ending {last4} for your complaint {ticket_id}. Value date: {value_date}.",
    "credit_rejected": "Dear customer, your request {ticket_id} could not be approved: {reason}. You can escalate by replying ESCALATE or calling 1800-000-0000.",
}


class NotifyArgs(BaseModel):
    case_id: str
    template: Literal["credit_approved", "credit_rejected"]
    fields: dict[str, str]


def _draft_notification(a: NotifyArgs) -> dict:
    """Drafts only; a human sends. Idempotent by case_id. Fields come from the case record (KB-TPL-NOTIFY)."""
    body = TEMPLATES[a.template].format(**a.fields)
    with connect() as conn:
        conn.execute("""INSERT INTO notifications (notification_id, case_id, channel, body) VALUES (%s, %s, 'sms', %s)
                        ON CONFLICT (case_id) DO NOTHING""", (f"N-{uuid.uuid4().hex[:10]}", a.case_id, body))
        row = conn.execute("SELECT notification_id, body, status FROM notifications WHERE case_id = %s", (a.case_id,)).fetchone()
        conn.commit()
    return {"notification_id": row[0], "body": row[1], "status": row[2], "template": a.template, "rule_id": "KB-TPL-NOTIFY"}


def draft_customer_notification(**kw) -> ToolResult:
    return run_tool(_draft_notification, NotifyArgs, kw)
