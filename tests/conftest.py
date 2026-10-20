"""Shared test setup: one throwaway, fully migrated Postgres database per test session.

A test that needs Postgres requests the ``database_url`` fixture. The first such request:

1. reads ``TEST_DATABASE_URL`` (default below) and refuses any database whose name does not start
   with ``fello_test``, because the next step drops it;
2. drops that database, so every session starts empty;
3. points ``DATABASE_URL`` at it and sets the dev-only role passwords unless already set;
4. runs ``migrate()``, which recreates the database, the roles and the schema.

If Postgres cannot be reached, every test that needs it is skipped. Tests that do not request
the fixture never touch a database. Each developer can point ``TEST_DATABASE_URL`` at
its own database, for example ``.../fello_test_platform``, so parallel runs never collide.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import make_url

from app.db import make_engine
from app.migrate import migrate

# 127.0.0.1 rather than localhost, as in .env.example (see the note there).
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/fello_test"

# The same dev-only values as .env.example. Roles are cluster-wide, so every local run must agree.
DEV_PASSWORDS = {
    "APP_OWNER_PASSWORD": "app_owner_dev",
    "APP_BATCH_PASSWORD": "app_batch_dev",
    "APP_PASSWORD": "app_dev",
    "PII_HASH_SECRET": "dev-only-pii-secret",
}


@pytest.fixture(scope="session")
def database_url() -> str:
    """Drop, recreate and migrate the test database once per session; return its admin URL."""
    url = make_url(os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL)
    if not (url.database or "").startswith("fello_test"):
        pytest.exit(
            f"refusing to drop database {url.database!r}: its name must start with fello_test",
            returncode=2,
        )

    maintenance = make_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",  # DROP DATABASE cannot run inside a transaction
        poolclass=sa.pool.NullPool,
    )
    try:
        with maintenance.connect() as conn:
            # FORCE ends connections left open by an earlier, interrupted session.
            conn.connection.driver_connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(url.database)
                )
            )
    except sa.exc.OperationalError as exc:
        pytest.skip(f"Postgres is unreachable: {exc}")
    finally:
        maintenance.dispose()

    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    # Never call a real LLM from a test, whatever .env holds; narration tests pass a fake client.
    os.environ["LLM_API_KEY"] = ""
    for key, value in DEV_PASSWORDS.items():
        os.environ.setdefault(key, value)
    migrate()  # creates the database again, then the roles and the schema
    return os.environ["DATABASE_URL"]
