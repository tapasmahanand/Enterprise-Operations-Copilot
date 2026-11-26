"""Read-only bank connector as an MCP server (D57).

    python -m kestrel.connectors.mcp_server          # stdio (spawned by the chat graph)

Two tools, both read-only, with typed inputs and outputs (the output schema is published in
tools/list): `transaction_lookup` and `ticket_lookup`. They wrap the existing query
functions in `kestrel.tools.bank`; credit posting is not here and never will be (it stays in
the approval worker behind the supervisor's decision).

Every call carries a signed scope (connectors/scope.py). The connector, not the caller,
enforces it: a lookup for an account, card or ticket outside the scope is refused with
`out_of_scope`, and results are filtered to the scope as well, so a broad query (card digits
shared by two customers) cannot return the other customer's rows.

For a separate deployment the same server would run over authenticated Streamable HTTP;
this repo only wires stdio for the local demo.
"""

from __future__ import annotations

import os
import time
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from kestrel.connectors.scope import ScopeError, verify
from kestrel.tools import bank

server = MCPServer("kestrel-bank-readonly",
                   instructions="Read-only Kestrel Bank lookups. Every call needs a scope_token from the copilot.")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


class Transaction(BaseModel):
    txn_id: str
    account_id: str
    card_id: str | None
    card_last4: str | None
    txn_date: date
    alert_sent_on: date | None
    channel: str
    merchant: str | None
    amount_inr: int
    status: str
    cross_border: bool
    product: str
    credit_limit_inr: int | None
    customer_id: str


class TransactionLookupResult(BaseModel):
    transactions: list[Transaction]
    scope_subject: str


class Ticket(BaseModel):
    ticket_id: str
    account_id: str
    opened_on: date
    category: str
    description: str
    status: str


class TicketLookupResult(BaseModel):
    tickets: list[Ticket]
    scope_subject: str


def _scope(token: str) -> dict:
    try:
        return verify(token)
    except ScopeError as e:
        raise ToolError(f"unauthorized: {e}")


def _fault(tool: str) -> None:
    """KESTREL_MCP_FAULT="slow:transaction_lookup:10" makes a tool hang (timeout tests)."""
    kind, _, rest = os.environ.get("KESTREL_MCP_FAULT", "").partition(":")
    name, _, secs = rest.partition(":")
    if kind == "slow" and name == tool:
        time.sleep(float(secs or 30))


@server.tool(annotations=READ_ONLY)
def transaction_lookup(scope_token: str, account_id: str | None = None,
                       card_last4: str | None = Field(default=None, pattern=r"^\d{4}$"),
                       amount_inr: int | None = None, days_back: int = Field(default=30, ge=1, le=180),
                       as_of: date | None = None) -> TransactionLookupResult:
    """Recent transactions for an account or a card (last 4 digits) inside the caller's scope."""
    _fault("transaction_lookup")
    sc = _scope(scope_token)
    accounts, cards = set(sc["accounts"]), set(sc["cards"])
    if account_id and account_id not in accounts:
        raise ToolError(f"out_of_scope: account {account_id} is not in this session's scope")
    if card_last4 and not account_id and card_last4 not in cards:
        raise ToolError(f"out_of_scope: card ending {card_last4} is not in this session's scope")
    res = bank.transaction_lookup(account_id=account_id, card_last4=card_last4, amount_inr=amount_inr,
                                  days_back=days_back, as_of=as_of)
    if not res.ok:
        raise ToolError(res.error or "lookup failed")
    rows = [r for r in res.data if r["account_id"] in accounts or (r["card_last4"] in cards)]
    return TransactionLookupResult(transactions=[Transaction(**r) for r in rows], scope_subject=sc["sub"])


@server.tool(annotations=READ_ONLY)
def ticket_lookup(scope_token: str, ticket_id: str | None = None,
                  account_id: str | None = None) -> TicketLookupResult:
    """Tickets by id or by account, inside the caller's scope."""
    _fault("ticket_lookup")
    sc = _scope(scope_token)
    accounts, tickets = set(sc["accounts"]), set(sc["tickets"])
    if account_id and account_id not in accounts:
        raise ToolError(f"out_of_scope: account {account_id} is not in this session's scope")
    if not ticket_id and not account_id:
        raise ToolError("need ticket_id or account_id")
    res = bank.ticket_lookup(ticket_id=ticket_id, account_id=account_id)
    if not res.ok:
        raise ToolError(res.error or "lookup failed")
    rows = [r for r in res.data if r["account_id"] in accounts or r["ticket_id"] in tickets]
    if ticket_id and res.data and not rows:
        raise ToolError(f"out_of_scope: ticket {ticket_id} is not in this session's scope")
    return TicketLookupResult(tickets=[Ticket(**r) for r in rows], scope_subject=sc["sub"])


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
