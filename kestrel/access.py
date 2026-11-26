"""Document access control for retrieval (D54).

Who may read a policy document is decided by two facts, both server-side:

* the document's `audience` (front-matter, copied onto every chunk at ingest): a list of
  groups such as `all_staff` or `fraud_operations`;
* the reader's groups: `all_staff` for every signed-in user, plus the rows in
  `user_groups` for that user (memberships are data in Postgres, never claims the client
  sends).

A reader sees a document when the two lists overlap. The check runs inside the SQL that
ranks chunks (see retrieval/search.py), so a document the reader may not see is never
returned to Python, never reaches the model's context, the citations or the traces.

Fail closed: a document without an audience is rejected at ingest, a chunk with an empty
audience matches nobody, and a principal with no groups sees nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

ALL_STAFF = "all_staff"

# Groups a document may name. Ingest rejects anything else, so a typo ("fraud_ops") can't
# silently make a document invisible to the people it was written for.
KNOWN_GROUPS = frozenset({ALL_STAFF, "fraud_operations", "compliance", "supervisors"})


@dataclass(frozen=True)
class Principal:
    """The reader a retrieval runs for. Built from a signed-in user (principal_for) or, for
    runs outside the API (evals, scripts), one of the explicit constants below."""
    user_id: str
    groups: frozenset[str]

    def can_read(self, audience: Iterable[str] | None) -> bool:
        return bool(self.groups & set(audience or ()))

    def as_state(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "groups": sorted(self.groups)}

    @classmethod
    def from_state(cls, d: dict[str, Any] | None) -> "Principal":
        """Inverse of as_state. A missing principal is a principal with no groups (sees nothing)."""
        if not d:
            return NOBODY
        return cls(str(d.get("user_id", "?")), frozenset(d.get("groups") or ()))


NOBODY = Principal("nobody", frozenset())
# Evals and the scope eval measure the tier-1 desk, so they read as a tier-1 agent.
TIER1 = Principal("system:tier1", frozenset({ALL_STAFF}))


def principal_for(user: Any) -> Principal:
    """From an auth.User (or anything with user_id and groups)."""
    return Principal(user.user_id, frozenset({ALL_STAFF, *getattr(user, "groups", ())}))


def visible_document_ids(conn: Any, principal: Principal, ids: Iterable[str]) -> set[str]:
    """Which of `ids` this principal may read. Unknown ids are treated as not visible."""
    ids = list({i for i in ids if i})
    if not ids:
        return set()
    rows = conn.execute("SELECT document_id FROM documents WHERE document_id = ANY(%s) AND audience && %s::text[]",
                        (ids, sorted(principal.groups))).fetchall()
    return {r[0] for r in rows}


def _doc_of(chunk_id: str) -> str:
    return chunk_id.rsplit(":", 1)[0]


def redact_chunk_refs(conn: Any, principal: Principal, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For evidence lists shown to someone other than the person who retrieved them (a
    supervisor reading an agent's case): chunks from documents the viewer can't read keep
    their place but lose id, title and score."""
    refs = [i for i in items if isinstance(i, dict) and i.get("chunk_id")]
    ok = visible_document_ids(conn, principal, (_doc_of(i["chunk_id"]) for i in refs))
    out = []
    for i in items:
        if isinstance(i, dict) and i.get("chunk_id") and _doc_of(i["chunk_id"]) not in ok:
            out.append({"chunk_id": "[restricted]", "title": "[document you are not cleared to read]"})
        else:
            out.append(i)
    return out
