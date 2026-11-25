"""Postgres checkpointer shared by both graphs. The case graph's pause at approval lives
here, not in process memory, which is what lets it survive an app restart."""

from __future__ import annotations

import atexit
from functools import lru_cache

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from kestrel.config import get_settings


@lru_cache(maxsize=1)
def get_checkpointer() -> PostgresSaver:
    pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    atexit.register(pool.close)  # otherwise interpreter exit waits ~20 s on pool threads
    saver = PostgresSaver(pool)
    saver.setup()  # idempotent: creates/migrates checkpoint tables
    return saver
