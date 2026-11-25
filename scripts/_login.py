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
