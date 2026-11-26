"""Signed lookup scope for the bank connector (D57).

The chat graph mints a scope from what the session has verified (the account on the agent's
screen, card digits the agent typed, earlier entities in this call), signs it with HMAC-SHA256
and sends it with every connector call. The connector verifies the signature and expiry and
refuses anything outside it. The model can choose a tool argument; it cannot widen the scope,
because it never sees the key.

The key comes from KESTREL_MCP_SCOPE_KEY. For the local stdio connector the client makes a
random one per process and hands it to the child it spawns, so nothing needs configuring.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any


class ScopeError(Exception):
    pass


def scope_key() -> bytes:
    k = os.environ.get("KESTREL_MCP_SCOPE_KEY")
    if not k:
        k = secrets.token_hex(32)
        os.environ["KESTREL_MCP_SCOPE_KEY"] = k   # inherited by the connector we spawn
    return k.encode()


def mint(*, subject: str, accounts: list[str] | None = None, cards: list[str] | None = None,
         tickets: list[str] | None = None, ttl_s: int = 120) -> str:
    body = {"sub": subject, "accounts": sorted({a for a in accounts or [] if a}),
            "cards": sorted({c for c in cards or [] if c}), "tickets": sorted({t for t in tickets or [] if t}),
            "exp": int(time.time()) + ttl_s}
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    sig = hmac.new(scope_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify(token: str) -> dict[str, Any]:
    try:
        raw, sig = token.rsplit(".", 1)
    except (ValueError, AttributeError):
        raise ScopeError("malformed scope token")
    want = hmac.new(scope_key(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        raise ScopeError("scope token signature invalid")
    body = json.loads(base64.urlsafe_b64decode(raw))
    if body.get("exp", 0) < time.time():
        raise ScopeError("scope token expired")
    return body
