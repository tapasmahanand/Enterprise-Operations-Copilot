import os
import uuid

import psycopg
import pytest

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel")


@pytest.fixture(scope="session")
def db_url():
    """Create a throwaway database for the session; skip if Postgres isn't reachable."""
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
