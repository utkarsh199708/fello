"""Bootstrap on the tiny config: a first scored night, then a second run that changes nothing.

Also checks that bootstrap refuses a database holding tenants its config does not name.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.config import load_config
from app.db import get_engine, make_engine
from app.jobs import cli
from app.jobs.bootstrap import bootstrap
from tests.helpers import TINY, purge_tenants

CFG = load_config(TINY)
IDS = [t.tenant_id for t in CFG.tenants]
TABLES = ("raw_events", "normalised_events", "quarantine", "snapshots", "scores", "job_runs")


def model_versions(admin_url) -> set[str]:
    engine = make_engine(admin_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            return set(conn.execute(sa.text("SELECT model_version FROM model_versions")).scalars())
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def data_dir(database_url, tmp_path_factory):
    """A data directory for the module. Teardown leaves the database as this module found it.

    That means purging the tiny tenants and also deleting the model bootstrap trained, as the
    admin user: test_db.py runs after this module and expects ``model_versions`` empty.
    """
    existing = model_versions(database_url)
    yield tmp_path_factory.mktemp("bootstrap")
    purge_tenants(database_url, IDS)
    engine = make_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM model_versions WHERE NOT (model_version = ANY(:keep))"),
                {"keep": sorted(existing)},
            )
    finally:
        engine.dispose()


def table_counts(admin_url):
    """Row counts of the tiny tenants per table, as the admin user (which bypasses RLS)."""
    engine = make_engine(admin_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            counts = {
                t: conn.execute(
                    sa.text(f"SELECT count(*) FROM {t} WHERE tenant_id = ANY(:ids)"),  # noqa: S608
                    {"ids": IDS},
                ).scalar_one()
                for t in TABLES
            }
            counts["active_model"] = conn.execute(
                sa.text("SELECT model_version FROM model_versions WHERE is_active")
            ).scalar_one()
            return counts
    finally:
        engine.dispose()


def test_first_bootstrap_scores_the_first_night(database_url, data_dir, capsys):
    runs = bootstrap(TINY, data_dir)
    assert [r.tenant_id for r in runs] == IDS
    assert [r.status for r in runs] == ["succeeded", "succeeded"]
    assert all(r.counts["accounts_scored"] > 0 for r in runs)
    out = capsys.readouterr().out
    for name in ("migrate", "generate", "load", "resolve", "anchors", "train", "nightly"):
        assert f"bootstrap: {name} done in" in out

    engine = make_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT tenant_id, trigger, status, as_of, model_version FROM job_runs"
                    " WHERE tenant_id = ANY(:ids) ORDER BY tenant_id"
                ),
                {"ids": IDS},
            ).all()
            scored = conn.execute(
                sa.text(
                    "SELECT count(*) FROM scores"
                    " WHERE tenant_id = ANY(:ids) AND as_of = :t AND run_id IS NOT NULL"
                ),
                {"ids": IDS, "t": CFG.train_as_of},
            ).scalar_one()
    finally:
        engine.dispose()
    assert len(rows) == 2
    assert {(r.trigger, r.status, r.as_of) for r in rows} == {
        ("bootstrap", "succeeded", CFG.train_as_of)
    }
    assert all(r.model_version for r in rows)
    assert scored == sum(r.counts["accounts_scored"] for r in runs)


def test_second_bootstrap_changes_nothing(database_url, data_dir, capsys):
    before = table_counts(database_url)
    manifest = (data_dir / "manifest.json").stat().st_mtime_ns
    assert cli.main(["bootstrap", "--config", str(TINY), "--data", str(data_dir)]) == 0
    assert "no_op" in capsys.readouterr().out
    assert table_counts(database_url) == before
    assert (data_dir / "manifest.json").stat().st_mtime_ns == manifest  # not regenerated


def test_refuses_a_database_with_other_tenants(database_url, data_dir):
    stray = uuid.uuid4()
    with get_engine("app").begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (tenant_id, slug, name, segment, top_k,"
                " contraction_threshold, connected_at)"
                " VALUES (:tid, 'stray', 'Stray', 'smb', 8, 0.3, '{}')"
            ),
            {"tid": stray},
        )
    try:
        with pytest.raises(RuntimeError, match="stray"):
            bootstrap(TINY, data_dir)
    finally:
        purge_tenants(database_url, [stray])
