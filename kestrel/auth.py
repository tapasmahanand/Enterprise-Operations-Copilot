"""Login, server-side sessions and the append-only audit log (D50-D52).

Who is calling comes only from a session token the server issued at login: an HttpOnly
cookie for the console, or `Authorization: Bearer <token>` for scripts. The role, and for
supervisors the approver identity (org_directory person_id, which sets the approval tier),
are read from the user's row. Nothing the client sends (no X-Role header, no approver_id
in a body) can choose them.

Server-side sessions instead of signed tokens (JWT): a session can be revoked at logout or
by deleting the row, and there is no signing key to manage. Only a SHA-256 of the token is
stored, so a database read does not hand out live sessions.

Passwords use scrypt from the standard library. The demo users share one password
(KESTREL_DEMO_PASSWORD, default "kestrel-demo"): fine for a local demo, not for anything else.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import HTTPException, Request
from psycopg.types.json import Jsonb

from kestrel.db import connect

log = logging.getLogger("auth")

COOKIE = "kestrel_session"
SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", 8 * 3600))   # one shift
ROLES = ("agent", "supervisor", "compliance")
DEFAULT_DEMO_PASSWORD = "kestrel-demo"

# user_id, display name, role, org_directory person_id (sets the approval tier; supervisors only)
DEMO_USERS = [
    ("agent1", "Priya Nair", "agent", None),
    ("agent2", "Sameer Kulkarni", "agent", None),
    ("ananya", "Ananya Rao", "supervisor", "P01"),     # team_lead
    ("farah", "Farah Qureshi", "supervisor", "P03"),   # ops_manager
    ("rohan", "Rohan Iyer", "supervisor", "P04"),      # regional_ops_head
    ("compliance1", "Kabir Sethi", "compliance", None),
]


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    role: str
    person_id: str | None
    approver_role: str | None = None   # org_directory role of person_id, e.g. team_lead

    def public(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "display_name": self.display_name, "role": self.role,
                "person_id": self.person_id, "approver_role": self.approver_role}


# --- passwords ---------------------------------------------------------------------------

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))   # same work for unknown users (no timing oracle)


def seed_users(conn: psycopg.Connection, password: str | None = None) -> int:
    """Create the demo users if missing. Never overwrites an existing user's password."""
    password = password or os.environ.get("KESTREL_DEMO_PASSWORD") or DEFAULT_DEMO_PASSWORD
    if password == DEFAULT_DEMO_PASSWORD:
        log.warning("demo users use the default password; set KESTREL_DEMO_PASSWORD outside a local demo")
    n = 0
    for user_id, name, role, person_id in DEMO_USERS:
        n += conn.execute(
            """INSERT INTO app_users (user_id, display_name, role, person_id, password_hash)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING""",
            (user_id, name, role, person_id, hash_password(password))).rowcount
    conn.commit()
    return n


# --- login throttling (in memory, per process) ----------------------------------------------

MAX_FAILURES, WINDOW_S = 5, 15 * 60
_FAILS: dict[str, list[float]] = {}
_FAILS_LOCK = threading.Lock()


def _throttled(user_id: str) -> bool:
    now = time.time()
    with _FAILS_LOCK:
        recent = [t for t in _FAILS.get(user_id, []) if now - t < WINDOW_S]
        _FAILS[user_id] = recent
        return len(recent) >= MAX_FAILURES


def _note_failure(user_id: str) -> None:
    with _FAILS_LOCK:
        _FAILS.setdefault(user_id, []).append(time.time())


# --- sessions ------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TooManyAttempts(Exception):
    pass


def login(user_id: str, password: str, ip: str | None = None) -> tuple[str, User] | None:
    """Returns (token, user) or None. Every attempt, good or bad, is written to the audit log."""
    user_id = user_id.strip().lower()
    if _throttled(user_id):
        with connect() as conn:
            audit(conn, None, "login_throttled", "user", user_id, {"ip": ip})
            conn.commit()
        raise TooManyAttempts(user_id)
    with connect() as conn:
        row = conn.execute("SELECT password_hash, active FROM app_users WHERE user_id = %s", (user_id,)).fetchone()
        ok = verify_password(password, row[0] if row else _DUMMY_HASH) and bool(row and row[1])
        if not ok:
            _note_failure(user_id)
            audit(conn, None, "login_failed", "user", user_id, {"ip": ip})
            conn.commit()
            return None
        token = secrets.token_urlsafe(32)
        conn.execute("""INSERT INTO auth_sessions (token_hash, user_id, expires_at)
                        VALUES (%s, %s, now() + make_interval(secs => %s))""",
                     (_token_hash(token), user_id, SESSION_TTL_S))
        user = _load_user(conn, user_id)
        audit(conn, user, "login", "user", user_id, {"ip": ip})
        conn.commit()
    return token, user


def logout(token: str, user: User) -> None:
    with connect() as conn:
        conn.execute("UPDATE auth_sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
                     (_token_hash(token),))
        audit(conn, user, "logout", "user", user.user_id)
        conn.commit()


def _load_user(conn: psycopg.Connection, user_id: str) -> User:
    r = conn.execute("""SELECT u.user_id, u.display_name, u.role, u.person_id, o.role
                        FROM app_users u LEFT JOIN org_directory o USING (person_id)
                        WHERE u.user_id = %s""", (user_id,)).fetchone()
    return User(*r)


def user_for_token(token: str) -> User | None:
    with connect() as conn:
        r = conn.execute("""SELECT u.user_id FROM auth_sessions s JOIN app_users u USING (user_id)
                            WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()
                              AND u.active""", (_token_hash(token),)).fetchone()
        return _load_user(conn, r[0]) if r else None


def token_from(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip() or None
    return request.cookies.get(COOKIE)


def current_user(request: Request) -> User:
    """FastAPI dependency: 401 unless the request carries a live session."""
    token = token_from(request)
    user = user_for_token(token) if token else None
    if user is None:
        raise HTTPException(401, "not signed in", headers={"WWW-Authenticate": "Bearer"})
    return user


def require(user: User, *roles: str) -> User:
    if user.role not in roles:
        raise HTTPException(403, f"{user.user_id} is a {user.role}; this needs {' or '.join(roles)}")
    return user


# --- audit log -----------------------------------------------------------------------------

def audit(conn: psycopg.Connection, actor: User | None, action: str, object_type: str, object_id: str | None,
          detail: dict[str, Any] | None = None) -> None:
    """Append one row to audit_log on the caller's connection, so it commits (or rolls back)
    with the change it describes. The table rejects UPDATE, DELETE and TRUNCATE (schema.sql)."""
    conn.execute(
        """INSERT INTO audit_log (actor_user_id, actor_role, actor_person_id, action, object_type, object_id, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (actor.user_id if actor else None, actor.role if actor else None, actor.person_id if actor else None,
         action, object_type, object_id, Jsonb(detail or {})))
