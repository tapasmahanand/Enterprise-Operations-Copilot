import os
import uuid

import psycopg
import pytest

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel")


@pytest.fixture(scope="module")
def db_url():
    """Create a throwaway database per test module (modules seed and ingest differently);
    skip if Postgres isn't reachable."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable; set TEST_ADMIN_DATABASE_URL")
    name = f"kestrel_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f"CREATE DATABASE {name}")
    base, _, _ = ADMIN_URL.rpartition("/")
    yield f"{base}/{name}"
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


def reset_graph_singletons():
    """The checkpointer pool and compiled graphs are process-wide singletons bound to the
    DATABASE_URL at first use. Each test module gets its own database, so a module that
    builds the app must drop them first or it talks to the previous module's (dropped) DB."""
    from kestrel.checkpoint import get_checkpointer
    import kestrel.graphs.case as case_graph
    import kestrel.graphs.chat as chat_graph
    get_checkpointer.cache_clear()
    case_graph._graph = None
    chat_graph._graph = None
