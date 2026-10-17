"""Database access: one engine per role, and the only place tenant context is set.

Three login roles share one database (docs/PLAN.md section 8, "RLS, real"):

- ``app_owner`` owns every table and runs migrations. Nothing else connects as it.
- ``app_batch`` has BYPASSRLS. The trainer, ``evaluate`` and ``rescore`` use it, as the deliberate
  cross-tenant readers.
- ``app`` is an ordinary role for web, worker and loader. Row-level security binds it.

Every tenant-scoped table has FORCE ROW LEVEL SECURITY and the policy
``tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid``. With no tenant context
that expression is NULL, so a query sees no rows and an insert is refused: the system fails closed.
``tenant_tx`` is the one function that sets the context, and only for the length of a transaction.

``DATABASE_URL`` is the admin (superuser) URL. The role URLs are derived from it, with each role's
password read from ``<ROLE>_PASSWORD``. The admin URL is used only by ``prepare_database``.
"""

from __future__ import annotations

import functools
import os
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import Connection, Engine, make_url

ROLES = ("app_owner", "app_batch", "app")

# Role attributes, applied on every prepare_database so the roles cannot drift from this list.
# Only app_batch may bypass row-level security. None is a superuser.
ROLE_ATTRIBUTES = {
    "app_owner": "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
    "app_batch": "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS",
    "app": "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
}


def admin_url() -> str:
    """Return ``DATABASE_URL`` with the driver SQLAlchemy needs.

    Many hosts issue ``postgresql://`` URLs (some tools write ``postgres://``). Without the
    ``+psycopg`` suffix SQLAlchemy would pick psycopg2, which is not installed.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def role_url(role: str) -> str:
    """Return the admin URL with the user and password swapped for ``role``.

    Fails loudly when the password is unset, so a process never falls back to the admin user.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    return (
        make_url(admin_url())
        .set(username=role, password=role_password(role))
        .render_as_string(hide_password=False)
    )


def role_password(role: str) -> str:
    """Read ``<ROLE>_PASSWORD`` from the environment, for example ``APP_BATCH_PASSWORD``."""
    key = f"{role.upper()}_PASSWORD"
    password = os.environ.get(key, "")
    if not password:
        raise RuntimeError(f"{key} is not set")
    return password


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine whose every connection runs with ``TimeZone=UTC``.

    timestamptz values then come back as UTC datetimes, whatever the server default is.
    ``connect_timeout`` turns an unreachable server into an error instead of a hang.
    ``pool_pre_ping`` replaces connections a proxy has silently closed while idle.
    """
    return sa.create_engine(
        url,
        connect_args={"options": "-c TimeZone=UTC", "connect_timeout": 10},
        pool_pre_ping=True,
        **kwargs,
    )


def get_engine(role: str = "app") -> Engine:
    """Return the shared engine for ``role``.

    Cached per URL, and the URL names the role, so each role gets one pool. Because the URL is
    read on every call, a test that repoints ``DATABASE_URL`` gets a fresh engine.
    """
    return _engine_for(role_url(role))


@functools.cache
def _engine_for(url: str) -> Engine:
    return make_engine(url)


@contextmanager
def tenant_tx(tenant_id: uuid.UUID | str, engine: Engine | None = None) -> Iterator[Connection]:
    """Open a transaction scoped to one tenant and yield its connection.

    ``set_config(..., true)`` is the bind-parameter form of ``SET LOCAL``: the setting ends with
    the transaction, so a pooled connection never carries one tenant's context into the next
    checkout. This is the only place in the code base that sets ``app.tenant_id``.
    The transaction commits when the block exits normally and rolls back on an exception.
    """
    tid = str(uuid.UUID(str(tenant_id)))  # rejects anything that is not a UUID
    engine = engine if engine is not None else get_engine("app")
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tid})
        yield conn


def copy_insert(
    conn: Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    on_conflict: str = "",
) -> int:
    """Bulk insert ``rows`` into ``table`` and return how many rows were inserted.

    Postgres refuses ``COPY FROM`` into a table with row-level security. So the rows are COPYed
    into a temporary stage with the same column types, then moved with ``INSERT ... SELECT``,
    where the policy's WITH CHECK still validates every row against the current tenant.

    Runs inside the caller's transaction (normally ``tenant_tx``) and never commits.
    ``on_conflict`` is appended verbatim, for example ``"ON CONFLICT DO NOTHING"``; callers pass
    a constant, never user input. Pass jsonb values as JSON text.
    """
    quote = conn.dialect.identifier_preparer.quote
    stage, target = quote(f"_stage_{table}"), quote(table)
    cols = ", ".join(quote(c) for c in columns)
    # WITH NO DATA copies the column types only: no constraints, defaults or identity.
    conn.exec_driver_sql(
        f"CREATE TEMP TABLE {stage} ON COMMIT DROP AS SELECT {cols} FROM {target} WITH NO DATA"
    )
    # COPY is the one step that needs the driver's own API.
    with conn.connection.driver_connection.cursor() as cur:
        with cur.copy(f"COPY {stage} ({cols}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    # Back through SQLAlchemy, so a policy or constraint violation raises its usual errors.
    inserted = conn.exec_driver_sql(
        f"INSERT INTO {target} ({cols}) SELECT {cols} FROM {stage} {on_conflict}"
    ).rowcount
    # Dropped now so the same transaction can call copy_insert again for the next batch.
    conn.exec_driver_sql(f"DROP TABLE {stage}")
    return inserted


def tenant_ids(
    tenant: str | uuid.UUID | None = None, engine: Engine | None = None
) -> list[uuid.UUID]:
    """Return every tenant id ordered by slug, or just the one named by ``tenant``.

    ``tenant`` may be a slug or a UUID. ``tenants`` has no row-level security, because it is the
    directory a job reads before any tenant context exists, so no context is needed here.
    Raises ``ValueError`` for a tenant that does not exist.
    """
    query = "SELECT tenant_id FROM tenants"
    params: dict[str, Any] = {}
    if tenant is not None:
        try:
            params["tid"] = uuid.UUID(str(tenant))
            query += " WHERE tenant_id = :tid"
        except ValueError:
            params["slug"] = str(tenant)
            query += " WHERE slug = :slug"
    engine = engine if engine is not None else get_engine("app")
    with engine.connect() as conn:
        ids = list(conn.execute(sa.text(query + " ORDER BY slug"), params).scalars())
    if tenant is not None and not ids:
        raise ValueError(f"unknown tenant {tenant!r}")
    return ids


def prepare_database() -> None:
    """Create the database and the three roles if missing, as the admin user. Idempotent.

    This runs before Alembic, because creating a BYPASSRLS role needs a superuser and the
    passwords come from the environment, never from a migration file. Roles are cluster-wide, so
    the work is serialised with an advisory lock in the ``postgres`` maintenance database: two
    processes migrating different databases on one server cannot race on the same role.
    Each run re-applies the attributes and the password, so the environment stays the source
    of truth.
    """
    target = make_url(admin_url())
    passwords = {role: role_password(role) for role in ROLES}  # fail before touching anything

    maintenance = make_engine(
        target.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",  # CREATE DATABASE cannot run inside a transaction
        poolclass=sa.pool.NullPool,
    )
    try:
        with maintenance.connect() as conn:
            # Session-level lock, released when this connection closes at the end of the block.
            conn.execute(sa.text("SELECT pg_advisory_lock(hashtext('app.prepare_database'))"))
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            raw = conn.connection.driver_connection
            if not exists:
                raw.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target.database)))
            for role in ROLES:
                _ensure_role(raw, role, passwords[role])
    finally:
        maintenance.dispose()

    # In Postgres 15+ only the schema owner may create in public. app_owner needs to, so that it
    # owns every table and FORCE ROW LEVEL SECURITY binds it too.
    admin = make_engine(admin_url(), poolclass=sa.pool.NullPool)
    try:
        with admin.begin() as conn:
            conn.execute(sa.text("GRANT USAGE, CREATE ON SCHEMA public TO app_owner"))
            conn.execute(sa.text("GRANT USAGE ON SCHEMA public TO app, app_batch"))
    finally:
        admin.dispose()


def _ensure_role(raw: psycopg.Connection, role: str, password: str) -> None:
    """Create ``role`` if missing, then set its attributes and password.

    CREATE ROLE and ALTER ROLE take no bind parameters, so psycopg quotes the name as an
    identifier and the password as a literal. ``DuplicateObject`` means another process created
    the role between our check and our CREATE.
    """
    exists = raw.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if not exists:
        try:
            raw.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        except psycopg.errors.DuplicateObject:
            pass
    raw.execute(
        sql.SQL("ALTER ROLE {} WITH " + ROLE_ATTRIBUTES[role] + " PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
