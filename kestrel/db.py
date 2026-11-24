"""Postgres connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector

from kestrel.config import ROOT, get_settings

SCHEMA = ROOT / "db" / "schema.sql"


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(url or get_settings().database_url) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        yield conn


def apply_schema(conn: psycopg.Connection, path: Path = SCHEMA) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()
