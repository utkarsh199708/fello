"""Platform tests: roles, row-level security, tenant_tx, copy_insert and migrate.

The two tenants this module creates are removed at module teardown by the admin user (a superuser
bypasses row-level security, and the runtime roles cannot DELETE), so later modules in the same
session see only their own tenants.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from app import db
from app.migrate import main as migrate_main
from app.migrate import migrate

TENANT_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")

ALL_TABLES = {
    "tenants",
    "model_versions",
    "csms",
    "accounts",
    "contracts",
    "crosswalk",
    "raw_events",
    "normalised_events",
    "quarantine",
    "snapshots",
    "scores",
    "job_runs",
    "tasks",
}
GLOBAL_TABLES = {"tenants", "model_versions"}

# Reverse dependency order, for the teardown purge.
PURGE_ORDER = [
    "tasks",
    "scores",
    "job_runs",
    "snapshots",
    "quarantine",
    "normalised_events",
    "raw_events",
    "crosswalk",
    "contracts",
    "accounts",
    "csms",
]

RAW_COLUMNS = (
    "tenant_id",
    "source",
    "source_event_id",
    "source_schema_version",
    "event_type",
    "occurred_at",
    "received_at",
    "payload",
)

RLS_ERROR = "row-level security"


@pytest.fixture(scope="module")
def admin(database_url):
    engine = db.make_engine(database_url, poolclass=sa.pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def tenants(admin):
    """Two tenants, inserted as ``app`` with no context: tenants is the one table that allows it."""
    with db.get_engine("app").begin() as conn:
        for tid, slug in ((TENANT_A, "dbtest-a"), (TENANT_B, "dbtest-b")):
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (tenant_id, slug, name, segment, top_k,"
                    " contraction_threshold) VALUES (:tid, :slug, :slug, 'smb', 8, 0.3)"
                ),
                {"tid": tid, "slug": slug},
            )
    yield TENANT_A, TENANT_B
    ids = [TENANT_A, TENANT_B]
    with admin.begin() as conn:
        for table in PURGE_ORDER:
            conn.execute(sa.text(f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"), {"ids": ids})
        conn.execute(sa.text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"), {"ids": ids})


def insert_csm(conn, tenant_id, name):
    """Insert one CSM with a fresh id and return the id."""
    csm_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO csms (csm_id, tenant_id, name, email)"
            " VALUES (:csm_id, :tid, :name, :name || '@dbtest.test')"
        ),
        {"csm_id": csm_id, "tid": tenant_id, "name": name},
    )
    return csm_id


def count(conn, table):
    return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def raw_row(tenant_id, n):
    ts = datetime(2026, 10, 1, n, tzinfo=UTC)
    payload = json.dumps({"account_id": f"001{n:015d}", "name": "Acme"})
    return (tenant_id, "salesforce", f"evt-{n}", 1, "account_upserted", ts, ts, payload)


# --- URLs, no database needed -------------------------------------------------------------


def test_admin_url_adds_the_psycopg_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    assert db.admin_url() == "postgresql+psycopg://u:p@h:5432/d"
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/d")
    assert db.admin_url() == "postgresql+psycopg://u:p@h:5432/d"


def test_role_url_swaps_user_and_fails_loudly_without_a_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:secret@h:5432/d")
    monkeypatch.setenv("APP_BATCH_PASSWORD", "batch-pw")
    assert db.role_url("app_batch") == "postgresql+psycopg://app_batch:batch-pw@h:5432/d"
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="APP_PASSWORD is not set"):
        db.role_url("app")
    with pytest.raises(ValueError, match="unknown role"):
        db.role_url("postgres")


def test_only_db_module_sets_tenant_context():
    """tenant_tx is the one place app.tenant_id is set. Any other setter would bypass it."""
    setter = re.compile(r"set_config\s*\(|\bSET\s+(LOCAL\s+)?app\.tenant_id", re.IGNORECASE)
    app_dir = Path(db.__file__).parent
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path != app_dir / "db.py" and setter.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- Schema and roles ---------------------------------------------------------------------


def test_roles_have_the_expected_attributes(admin):
    with admin.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT rolname, rolsuper, rolcanlogin, rolbypassrls FROM pg_roles"
                " WHERE rolname IN ('app_owner', 'app_batch', 'app')"
            )
        ).all()
    attrs = {name: (sup, login, bypass) for name, sup, login, bypass in rows}
    assert attrs == {
        "app_owner": (False, True, False),
        "app_batch": (False, True, True),
        "app": (False, True, False),
    }


def test_every_tenant_table_has_forced_rls_and_the_policy(admin):
    with admin.connect() as conn:
        tables = dict(
            conn.execute(
                sa.text(
                    "SELECT c.relname, r.rolname FROM pg_class c JOIN pg_roles r"
                    " ON r.oid = c.relowner WHERE c.relkind = 'r'"
                    " AND c.relnamespace = 'public'::regnamespace"
                    " AND c.relname <> 'alembic_version'"
                )
            ).all()
        )
        tenant_tables = {
            name: nullable
            for name, nullable in conn.execute(
                sa.text(
                    "SELECT table_name, is_nullable FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                )
            ).all()
        }
        rls = {
            name: (enabled, forced)
            for name, enabled, forced in conn.execute(
                sa.text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
                )
            ).all()
        }
        policies = {
            table: (name, qual, check)
            for table, name, qual, check in conn.execute(
                sa.text(
                    "SELECT tablename, policyname, qual, with_check FROM pg_policies"
                    " WHERE schemaname = 'public'"
                )
            ).all()
        }

    assert set(tables) == ALL_TABLES
    assert set(tables.values()) == {"app_owner"}  # so FORCE binds the owner as well
    # tenants has tenant_id too, as its primary key. model_versions is the one table without it.
    assert set(tenant_tables) == ALL_TABLES - {"model_versions"}
    assert set(tenant_tables.values()) == {"NO"}
    for table in ALL_TABLES:
        if table in GLOBAL_TABLES:
            assert rls[table] == (False, False), table
            assert table not in policies, table
        else:
            assert rls[table] == (True, True), table
            name, qual, check = policies[table]
            assert name == "tenant_isolation"
            assert qual == check and "current_setting('app.tenant_id'::text, true)" in qual


def test_runtime_roles_cannot_delete_or_truncate(admin):
    with admin.connect() as conn:
        granted = conn.execute(
            sa.text(
                "SELECT r.rname, t.tname, p.priv FROM unnest(ARRAY['app', 'app_batch']) r(rname)"
                " CROSS JOIN unnest(CAST(:tables AS text[])) t(tname)"
                " CROSS JOIN unnest(ARRAY['DELETE', 'TRUNCATE']) p(priv)"
                " WHERE has_table_privilege(r.rname, t.tname, p.priv)"
            ),
            {"tables": sorted(ALL_TABLES)},
        ).all()
    assert granted == []


def test_job_runs_is_unique_per_job_tenant_and_night(admin):
    with admin.connect() as conn:
        definitions = conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'job_runs'::regclass AND contype = 'u'"
            )
        ).scalars()
        assert "UNIQUE (job_name, tenant_id, run_date)" in list(definitions)


def test_at_most_one_active_model(database_url):
    row = (
        "INSERT INTO model_versions (model_version, kind, feature_set_version, features,"
        " artifact, train_as_of, label_cutoff, anchor_first, anchor_last, tenant_row_counts,"
        " is_active) VALUES (:v, 'logreg', 'fs1', '[]', '{}', now(), now(), now(), now(),"
        " '{}', true)"
    )
    with db.get_engine("app_batch").connect() as conn:
        conn.execute(sa.text(row), {"v": "test-1"})
        with pytest.raises(sa.exc.IntegrityError, match="model_versions_one_active"):
            conn.execute(sa.text(row), {"v": "test-2"})
        conn.rollback()  # leaves model_versions empty for the modules that follow


def test_connections_run_in_utc(database_url):
    with db.get_engine("app").connect() as conn:
        assert conn.execute(sa.text("SHOW TimeZone")).scalar_one() == "UTC"


def test_migrate_is_idempotent(admin):
    migrate()
    assert migrate_main(["migrate"]) == 0  # the `python -m app migrate` entry point
    with admin.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0001"
        )
        assert conn.execute(
            sa.text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        ).scalar_one() == len(ALL_TABLES - GLOBAL_TABLES)


# --- Row-level security in use ------------------------------------------------------------


def test_no_context_gives_zero_rows_except_for_app_batch(tenants):
    a, b = tenants
    with db.tenant_tx(a) as conn:
        insert_csm(conn, a, "ann")
    with db.tenant_tx(b) as conn:
        insert_csm(conn, b, "bob")

    with db.get_engine("app").connect() as conn:
        assert count(conn, "csms") == 0
    # FORCE ROW LEVEL SECURITY: even the owner sees nothing without a tenant context.
    with db.get_engine("app_owner").connect() as conn:
        assert count(conn, "csms") == 0
    with db.tenant_tx(a) as conn:
        assert conn.execute(sa.text("SELECT DISTINCT tenant_id FROM csms")).scalars().all() == [a]
    # The trainer is the one deliberate cross-tenant reader.
    with db.get_engine("app_batch").connect() as conn:
        seen = conn.execute(sa.text("SELECT DISTINCT tenant_id FROM csms")).scalars().all()
        assert {a, b} <= set(seen)


def test_insert_without_context_is_refused(tenants):
    a, _ = tenants
    with pytest.raises(sa.exc.DBAPIError, match=RLS_ERROR):
        with db.get_engine("app").begin() as conn:
            insert_csm(conn, a, "nobody")


def test_tenant_context_is_transaction_local(tenants):
    """After tenant_tx ends, the same pooled connection carries no tenant context."""
    a, _ = tenants
    engine = db.make_engine(db.role_url("app"), pool_size=1, max_overflow=0)
    try:
        with db.tenant_tx(a, engine) as conn:
            insert_csm(conn, a, "carol")
            pid = conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
            inside = conn.execute(sa.text("SELECT current_setting('app.tenant_id')")).scalar_one()
            assert inside == str(a)
            assert count(conn, "csms") > 0
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one() == pid
            after = conn.execute(sa.text("SELECT current_setting('app.tenant_id', true)"))
            assert after.scalar_one() in ("", None)
            assert count(conn, "csms") == 0
    finally:
        engine.dispose()


def test_with_check_rejects_another_tenants_row(tenants):
    a, b = tenants
    with pytest.raises(sa.exc.DBAPIError, match=RLS_ERROR):
        with db.tenant_tx(a) as conn:
            insert_csm(conn, b, "intruder")
    with db.tenant_tx(b) as conn:
        names = conn.execute(sa.text("SELECT name FROM csms")).scalars().all()
    assert "intruder" not in names


def test_tenant_tx_rolls_back_on_error(tenants):
    a, _ = tenants
    with pytest.raises(RuntimeError):
        with db.tenant_tx(a) as conn:
            insert_csm(conn, a, "ghost")
            raise RuntimeError("boom")
    with db.tenant_tx(a) as conn:
        assert "ghost" not in conn.execute(sa.text("SELECT name FROM csms")).scalars().all()


def test_tenant_tx_rejects_a_non_uuid():
    with pytest.raises(ValueError):
        with db.tenant_tx("not-a-uuid"):
            pass


# --- copy_insert --------------------------------------------------------------------------


def test_copy_insert_works_under_rls(tenants):
    a, b = tenants
    with db.tenant_tx(a) as conn:
        # Two batches in one transaction: the stage table is dropped and made again.
        assert db.copy_insert(conn, "raw_events", RAW_COLUMNS, [raw_row(a, 1), raw_row(a, 2)]) == 2
        assert db.copy_insert(conn, "raw_events", RAW_COLUMNS, [raw_row(a, 3)]) == 1

    with db.tenant_tx(a) as conn:
        rows = conn.execute(
            sa.text("SELECT source_event_id, payload, received_at FROM raw_events ORDER BY 1")
        ).all()
    assert [r.source_event_id for r in rows] == ["evt-1", "evt-2", "evt-3"]
    assert rows[0].payload == {"account_id": "001000000000000001", "name": "Acme"}
    assert rows[0].received_at == datetime(2026, 10, 1, 1, tzinfo=UTC)
    with db.tenant_tx(b) as conn:
        assert count(conn, "raw_events") == 0


def test_copy_insert_rejects_another_tenants_row(tenants):
    a, b = tenants
    with pytest.raises(sa.exc.DBAPIError, match=RLS_ERROR):
        with db.tenant_tx(b) as conn:
            db.copy_insert(conn, "raw_events", RAW_COLUMNS, [raw_row(b, 4), raw_row(a, 5)])
    # The whole batch rolled back, including tenant B's own row.
    with db.tenant_tx(b) as conn:
        assert count(conn, "raw_events") == 0


def test_copy_insert_on_conflict_do_nothing(tenants):
    a, _ = tenants
    account_id = uuid.uuid4()
    columns = ("tenant_id", "source", "external_id", "account_id")
    rows = [(a, "stripe", "cus_1", account_id), (a, "zendesk", "org_1", account_id)]
    with db.tenant_tx(a) as conn:
        conn.execute(
            sa.text(
                "INSERT INTO accounts (account_id, tenant_id, name, started_at)"
                " VALUES (:id, :tid, 'Acme', '2025-01-01')"
            ),
            {"id": account_id, "tid": a},
        )
        assert db.copy_insert(conn, "crosswalk", columns, rows) == 2
        again = db.copy_insert(
            conn, "crosswalk", columns, rows, on_conflict="ON CONFLICT DO NOTHING"
        )
        assert again == 0


# --- tenant_ids ---------------------------------------------------------------------------


def test_tenant_ids_by_slug_or_uuid(tenants):
    a, b = tenants
    assert [t for t in db.tenant_ids() if t in (a, b)] == [a, b]  # ordered by slug
    assert db.tenant_ids("dbtest-b") == [b]
    assert db.tenant_ids(str(a)) == [a]
    assert db.tenant_ids(a) == [a]
    with pytest.raises(ValueError, match="unknown tenant"):
        db.tenant_ids("no-such-tenant")
