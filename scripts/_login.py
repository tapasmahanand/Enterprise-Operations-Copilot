"""Sign the demo scripts in as seeded users (D50). Each user gets a Bearer header."""

from __future__ import annotations

import os

import httpx

PASSWORD = os.environ.get("KESTREL_DEMO_PASSWORD", "kestrel-demo")
# Which supervisor approves a case of each tier (seeded users, kestrel/auth.py).
APPROVER_FOR_TIER = {"team_lead": "ananya", "ops_manager": "farah", "regional_ops_head": "rohan"}


def as_user(client: httpx.Client, user: str) -> dict[str, str]:
    r = client.post("/auth/login", json={"username": user, "password": PASSWORD})
    r.raise_for_status()
    client.cookies.clear()   # use the header only, so one client can act as several users
    return {"Authorization": f"Bearer {r.json()['token']}"}


def settle(client: httpx.Client, headers: dict[str, str], case: dict, timeout: float = 30) -> dict:
    """A decision returns as soon as it is committed; with the events service (D56) a consumer
    executes it a moment later. Poll the case until it leaves the in-flight states."""
    import time
    end = time.time() + timeout
    while case.get("status") in ("pending_approval", "approved") and (case.get("approval") or {}).get("state") != "pending" \
            and time.time() < end:
        time.sleep(0.5)
        case = client.get(f"/cases/{case['case_id']}", headers=headers).json()
    return case
