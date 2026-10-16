"""Row-level security isolation, checked against every tenant-scoped table in the catalog.

The list of tables comes from ``information_schema`` (every table with a ``tenant_id`` column,
``tenants`` itself excluded) rather than from a constant, so a table added later without a
policy fails here instead of passing silently. Two tenants are seeded and every table is made
non-empty for both, because a test over empty tables proves nothing.

1. Under tenant A the runtime role sees no row of tenant B, on every table, in both directions.
2. With no tenant context the runtime role sees zero rows on every table, including on a pooled
   connection that has just served a tenant.
3. The write side: a nightly run for tenant A changes no row count of tenant B on any table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.db import get_engine, tenant_tx
from app.ingest.resolve import resolve
from app.jobs.nightly import run_nightly
from tests.helpers import purge_tenants, seed_tiny

NIGHT = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)

TENANT_TABLES_SQL = """
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
      AND t.table_type = 'BASE TABLE' AND c.table_name <> 'tenants'
    ORDER BY c.table_name
"""


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    """Two tenants with rows in every scoped table, including job_runs and tasks."""
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    for tid in ids:
        insert_quarantined_event(tid)
    run_nightly(NIGHT)  # fills job_runs, and scores that carry a run_id
    for tid in ids:
        insert_task(tid)
    yield ids
    purge_tenants(database_url, ids)


def insert_quarantined_event(tenant_id) -> None:
    """The generator makes no identity breaks yet, so add an event no account can claim."""
    payload = '{"organization_id": "no-such-org", "ticket_id": 1, "requester_email": null}'
    with tenant_tx(tenant_id) as conn:
        conn.execute(
            sa.text(
                "INSERT INTO raw_events (tenant_id, source, source_event_id, source_schema_version,"
                " event_type, occurred_at, received_at, payload) VALUES (:tid, 'zendesk',"
                " 'qa-orphan-1', 1, 'ticket_created', :t, :t, CAST(:p AS jsonb))"
            ),
            {"tid": tenant_id, "t": NIGHT, "p": payload},
        )
        assert resolve(conn, tenant_id, NIGHT).by_reason == {"unknown_native_key": 1}


def insert_task(tenant_id) -> None:
    """tasks stays empty until Step 5, so write one per tenant through the normal runtime path."""
    with tenant_tx(tenant_id) as conn:
        score = conn.execute(
            sa.text(
                "SELECT score_id, account_id, run_id FROM scores WHERE run_id IS NOT NULL LIMIT 1"
            )
        ).one()
        conn.execute(
            sa.text(
                "INSERT INTO tasks (task_id, tenant_id, account_id, run_date, score_id, run_id,"
                " rule_fired, policy_version)"
                " VALUES (gen_random_uuid(), :tid, :acc, DATE '2026-10-13', :score, :run,"
                " 'qa_probe', 'qa')"
            ),
            {
                "tid": tenant_id,
                "acc": score.account_id,
                "score": score.score_id,
                "run": score.run_id,
            },
        )


def scoped_tables() -> list[str]:
    with get_engine("app").connect() as conn:
        return list(conn.execute(sa.text(TENANT_TABLES_SQL)).scalars())


def count(conn, table: str, where: str = "", **params) -> int:
    # Table names come from the catalog query above, never from user input.
    sql = f"SELECT count(*) FROM {table} {where}"  # noqa: S608
    return conn.execute(sa.text(sql), params).scalar_one()


def test_every_scoped_table_has_forced_row_level_security(seeded):
    """RLS is enabled and forced on each table with a tenant_id, so even the owner is bound."""
    tables = scoped_tables()
    # The schema has 11 scoped tables today; a new one joins the checks below automatically.
    assert len(tables) >= 11
    assert {"accounts", "snapshots", "scores", "job_runs", "tasks"} <= set(tables)
    with get_engine("app").connect() as conn:
        flags = dict(
            conn.execute(
                sa.text(
                    "SELECT relname, relrowsecurity AND relforcerowsecurity"
                    " FROM pg_class WHERE relname = ANY(:names) AND relkind = 'r'"
                ),
                {"names": tables},
            ).all()
        )
    assert flags == {t: True for t in tables}


def test_tenant_context_hides_the_other_tenant_on_every_table(seeded, database_url):
    """Under A: no row of B, and every row of A is there (so an empty table cannot pass)."""
    a_id, b_id = seeded
    admin = sa.create_engine(database_url)
    try:
        with admin.connect() as conn:  # a superuser bypasses RLS: the ground truth
            truth = {
                (table, tid): count(conn, table, "WHERE tenant_id = :tid", tid=tid)
                for table in scoped_tables()
                for tid in (a_id, b_id)
            }
    finally:
        admin.dispose()

    for mine, other in ((a_id, b_id), (b_id, a_id)):
        with tenant_tx(mine) as conn:
            for table in scoped_tables():
                assert truth[(table, mine)] > 0, f"{table} has no rows for the tenant: vacuous"
                leaked = count(conn, table, "WHERE tenant_id = :other", other=other)
                assert leaked == 0, f"{table}: tenant {mine} can see tenant {other}"
                assert count(conn, table) == truth[(table, mine)], f"{table}: visible rows differ"


def test_no_tenant_context_sees_zero_rows_on_every_table(seeded):
    """Fail closed: no context, no rows. Also right after a tenant transaction on the same pool."""
    engine = get_engine("app")
    with tenant_tx(seeded[0]) as conn:  # warm a pooled connection with a tenant context
        assert count(conn, "accounts") > 0
    for table in scoped_tables():
        with engine.connect() as conn:
            assert count(conn, table) == 0, table
        # The setting present but empty is the state after a transaction-local setting ends.
        with engine.begin() as conn:
            conn.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
            assert count(conn, table) == 0, table


def test_nightly_for_one_tenant_writes_nothing_for_the_other(seeded, database_url):
    """PLAN section 9: 'nightly for A writes nothing for B', checked on every scoped table.

    Counts come from an admin connection (it bypasses RLS) so a stray write is seen even if the
    runtime role could not read it back. The guard that A's counts rise keeps the test from
    passing because the run silently did nothing.
    """
    a_id, b_id = seeded
    tables = scoped_tables()

    def counts(tenant_id) -> dict[str, int]:
        admin = sa.create_engine(database_url)
        try:
            with admin.connect() as conn:
                return {t: count(conn, t, "WHERE tenant_id = :tid", tid=tenant_id) for t in tables}
        finally:
            admin.dispose()

    b_before, a_before = counts(b_id), counts(a_id)
    runs = run_nightly(NIGHT + timedelta(days=1), tenant=a_id)
    assert [(r.tenant_id, r.status) for r in runs] == [(a_id, "succeeded")]
    b_after, a_after = counts(b_id), counts(a_id)

    assert b_after == b_before
    assert a_after["job_runs"] == a_before["job_runs"] + 1
    assert a_after["scores"] > a_before["scores"]
