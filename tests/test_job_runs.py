"""job_runs uniqueness and the no-op rerun, against the seeded tiny tenants.

The unique key (job_name, tenant_id, run_date) is what makes a rerun safe: a second claim for
the same night cannot create a second row, and a succeeded night is not executed again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from app.db import tenant_tx
from app.jobs.nightly import run_id_for, run_nightly
from tests.helpers import purge_tenants, seed_tiny

NIGHT = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
LATER_SAME_DAY = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)

INSERT = sa.text(
    "INSERT INTO job_runs (run_id, job_name, tenant_id, run_date, as_of, trigger, status)"
    " VALUES (gen_random_uuid(), :job, :tid, :d, :as_of, 'cli', 'running')"
)


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def test_duplicate_job_run_for_the_same_night_is_rejected(seeded):
    tid, other = seeded
    params = {"job": "qa_probe", "tid": tid, "d": date(2026, 10, 14), "as_of": NIGHT}
    with tenant_tx(tid) as conn:
        conn.execute(INSERT, params)
    # A second row for the same job, tenant and date violates the unique key, whatever its run_id.
    with pytest.raises(sa.exc.IntegrityError, match="job_runs_job_name_tenant_id_run_date_key"):
        with tenant_tx(tid) as conn:
            conn.execute(INSERT, params)
    # The key is narrow: another date, another job name and another tenant are all allowed.
    with tenant_tx(tid) as conn:
        conn.execute(INSERT, {**params, "d": date(2026, 10, 15)})
        conn.execute(INSERT, {**params, "job": "qa_probe_2"})
    with tenant_tx(other) as conn:
        conn.execute(INSERT, {**params, "tid": other})
    with tenant_tx(tid) as conn:
        rows = conn.execute(
            sa.text("SELECT run_date FROM job_runs WHERE job_name = 'qa_probe' ORDER BY run_date")
        ).scalars()
        assert list(rows) == [date(2026, 10, 14), date(2026, 10, 15)]  # the duplicate left nothing


def test_rerun_of_a_succeeded_night_is_a_no_op_that_writes_nothing(seeded):
    first = run_nightly(NIGHT)
    assert [r.status for r in first] == ["succeeded", "succeeded"]

    def state(tid):
        with tenant_tx(tid) as conn:
            nightly = conn.execute(
                sa.text("SELECT * FROM job_runs WHERE job_name = 'nightly' ORDER BY run_date")
            ).all()
            tables = ("normalised_events", "snapshots", "scores")
            counts = [
                conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608
                for t in tables
            ]
            return nightly, counts

    before = {tid: state(tid) for tid in seeded}
    assert all(rows and counts[1] > 0 for rows, counts in before.values())
    # The same instant, and a later instant on the same UTC date: both are the same night.
    for as_of in (NIGHT, LATER_SAME_DAY):
        again = run_nightly(as_of)
        assert [r.status for r in again] == ["no_op", "no_op"]
        assert [r.run_id for r in again] == [run_id_for(t, NIGHT.date()) for t in seeded]
        assert [r.run_id for r in again] == [r.run_id for r in first]
    assert {tid: state(tid) for tid in seeded} == before  # rows, timings and counts untouched
