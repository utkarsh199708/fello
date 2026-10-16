"""Nightly job against the seeded tiny tenants.

Asserts one job_runs row per tenant per night with counts, timings and the model version; that a
rerun of a succeeded night is a no_op that writes nothing; and that a failing tenant is recorded
as failed without stopping the others, then re-executes under the same run_id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.db import get_engine, tenant_tx
from app.jobs import cli, nightly
from app.jobs.nightly import run_id_for, run_nightly
from tests.helpers import purge_tenants, seed_tiny

# Nights after the tiny config's train_as_of (Monday 2026-10-12), so each writes nightly rows.
TUESDAY = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
WEDNESDAY = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
THURSDAY = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    """Seed the tiny tenants; purge them afterwards, since tenant slugs are unique per database."""
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def job_row(tenant_id, run_date):
    with tenant_tx(tenant_id) as conn:
        return conn.execute(
            sa.text("SELECT * FROM job_runs WHERE job_name = 'nightly' AND run_date = :d"),
            {"d": run_date},
        ).one_or_none()


def count(tenant_id, sql, **params):
    with tenant_tx(tenant_id) as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


def test_one_job_runs_row_per_tenant(seeded):
    runs = run_nightly(TUESDAY)
    assert [r.tenant_id for r in runs] == seeded  # tenant_ids orders by slug: t1, t2
    assert [r.status for r in runs] == ["succeeded", "succeeded"]

    with get_engine("app").connect() as conn:
        active = conn.execute(
            sa.text("SELECT model_version FROM model_versions WHERE is_active")
        ).scalar_one()
    for run in runs:
        row = job_row(run.tenant_id, TUESDAY.date())
        assert row.run_id == run.run_id == run_id_for(run.tenant_id, TUESDAY.date())
        assert (row.status, row.trigger, row.as_of) == ("succeeded", "cli", TUESDAY)
        assert row.model_version == active and row.error is None and row.finished_at
        assert row.ingest_watermark <= TUESDAY
        assert row.counts == run.counts
        assert set(row.counts) == {
            "events_normalised",
            "events_quarantined",
            "snapshots_written",
            "accounts_scored",
        }
        assert set(row.stage_ms) == {"ingest", "snapshot", "score"}

        # The counts are the rows this night actually wrote.
        resolved = count(
            run.tenant_id,
            "SELECT count(*) FROM normalised_events WHERE resolved_as_of = :t",
            t=TUESDAY,
        )
        snapshots = count(
            run.tenant_id,
            "SELECT count(*) FROM snapshots WHERE as_of = :t AND kind = 'nightly'",
            t=TUESDAY,
        )
        scores = count(
            run.tenant_id, "SELECT count(*) FROM scores WHERE as_of = :t AND run_id = :r",
            t=TUESDAY, r=run.run_id,
        )  # fmt: skip
        assert resolved == row.counts["events_normalised"]
        assert snapshots == row.counts["snapshots_written"] > 0
        assert scores == row.counts["accounts_scored"] == snapshots


def test_rerun_of_a_succeeded_night_is_a_no_op(seeded):
    before = [job_row(t, TUESDAY.date()) for t in seeded]
    # Same as_of, and a later time on the same day: both are the same night.
    for as_of in (TUESDAY, TUESDAY + timedelta(hours=3)):
        runs = run_nightly(as_of, trigger="schedule")
        assert [r.status for r in runs] == ["no_op", "no_op"]
    assert [job_row(t, TUESDAY.date()) for t in seeded] == before
    for t in seeded:
        later = TUESDAY + timedelta(hours=3)
        assert count(t, "SELECT count(*) FROM snapshots WHERE as_of = :t", t=later) == 0


def test_failing_tenant_does_not_stop_the_others(seeded, monkeypatch):
    bad, good = seeded
    real_score = nightly.score_tenant

    def score_or_fail(conn, tenant_id, as_of, run_id=None):
        if tenant_id == bad:
            raise RuntimeError("malformed data for this tenant")
        return real_score(conn, tenant_id, as_of, run_id)

    monkeypatch.setattr(nightly, "score_tenant", score_or_fail)
    runs = run_nightly(WEDNESDAY)
    assert [(r.tenant_id, r.status) for r in runs] == [(bad, "failed"), (good, "succeeded")]
    assert "malformed data" in runs[0].error

    failed = job_row(bad, WEDNESDAY.date())
    assert failed.status == "failed" and "malformed data" in failed.error
    assert failed.counts == {} and failed.model_version is None and failed.finished_at
    assert set(failed.stage_ms) == {"ingest", "snapshot"}  # the score stage never finished
    # The work transaction rolled back: nothing of that night survives for the failed tenant.
    assert count(bad, "SELECT count(*) FROM snapshots WHERE as_of = :t", t=WEDNESDAY) == 0
    assert count(bad, "SELECT count(*) FROM normalised_events WHERE resolved_as_of = :t",
                 t=WEDNESDAY) == 0  # fmt: skip
    assert job_row(good, WEDNESDAY.date()).status == "succeeded"

    # Fixed: the failed night is claimed again under the same run_id; the good one is a no_op.
    monkeypatch.setattr(nightly, "score_tenant", real_score)
    runs = run_nightly(WEDNESDAY)
    assert [r.status for r in runs] == ["succeeded", "no_op"]
    retried = job_row(bad, WEDNESDAY.date())
    assert retried.run_id == failed.run_id
    assert retried.status == "succeeded" and retried.error is None
    assert retried.counts["accounts_scored"] > 0


def test_cli_runs_one_tenant(seeded, capsys):
    assert cli.main(["nightly", "--as-of", "2026-10-15T02:00:00Z", "--tenant", "t2"]) == 0
    assert "succeeded" in capsys.readouterr().out
    assert job_row(seeded[1], THURSDAY.date()).status == "succeeded"
    assert job_row(seeded[0], THURSDAY.date()) is None


def test_bad_arguments_are_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        run_nightly(datetime(2026, 10, 13, 2, 0))
    with pytest.raises(ValueError, match="trigger"):
        run_nightly(TUESDAY, trigger="cron")
