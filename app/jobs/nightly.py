"""Nightly job v0: for each tenant, ingest, snapshot and score at ``as_of``, recorded in job_runs.

The unit of work is one (tenant, night). Each tenant goes through three short transactions of its
own, all inside ``tenant_tx`` so row-level security binds every read and write:

1. **Claim.** Insert the tenant's ``job_runs`` row for ``run_date = as_of.date()`` with status
   ``running``. The row is unique on (job_name, tenant_id, run_date). If it already exists and
   ``succeeded``, nothing is written and the tenant's result is ``no_op``: a night that has rows
   is never rewritten. A ``failed`` row, or a ``running`` row left behind by a crash, is claimed
   again and re-executed under the same ``run_id``.
2. **Work.** ``resolve`` the events received by ``as_of``, write the nightly snapshot, score it.
   One transaction, so a failure part-way leaves no half-written night behind.
3. **Finish.** Record ``succeeded`` or ``failed`` with counts, stage timings and the error text.
   This is a separate transaction so that a failed night is still recorded after the work rolled
   back.

Each tenant has its own try/except: one tenant's bad data fails that tenant's row, not the run.

Time comes only from ``as_of``. ``perf_counter`` is a stopwatch for ``stage_ms``, never "now":
no logic depends on it. ``started_at`` and ``finished_at`` are filled by the database's
``now()`` and are audit columns only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import tenant_ids, tenant_tx
from app.ids import stable_uuid
from app.ingest.resolve import ingest_watermark, resolve
from app.ml.features import write_snapshots
from app.ml.score import active_model, score_tenant

log = logging.getLogger(__name__)

JOB_NAME = "nightly"
TRIGGERS = ("schedule", "cli", "bootstrap", "manual")  # the job_runs.trigger CHECK list
MAX_ERROR_CHARS = 2000  # enough for the exception line; the full traceback goes to the log

# On conflict, only a failed or a running row is taken over. A succeeded row makes the WHERE
# false, the statement updates nothing and RETURNING gives no row: that is the no_op signal.
CLAIM_SQL = sa.text(
    """
    INSERT INTO job_runs (run_id, job_name, tenant_id, run_date, as_of, trigger, status)
    VALUES (:run_id, :job_name, :tid, :run_date, :as_of, :trigger, 'running')
    ON CONFLICT (job_name, tenant_id, run_date) DO UPDATE
    SET status = 'running', as_of = EXCLUDED.as_of, trigger = EXCLUDED.trigger,
        model_version = NULL, ingest_watermark = NULL, counts = '{}', stage_ms = '{}',
        error = NULL, started_at = now(), finished_at = NULL
    WHERE job_runs.status IN ('failed', 'running')
    RETURNING run_id
    """
)

FINISH_SQL = sa.text(
    """
    UPDATE job_runs
    SET status = :status, model_version = :model_version, ingest_watermark = :watermark,
        counts = CAST(:counts AS jsonb), stage_ms = CAST(:stage_ms AS jsonb), error = :error,
        finished_at = now()
    WHERE run_id = :run_id
    """
)


@dataclass(frozen=True)
class TenantRun:
    """What happened to one tenant: ``succeeded``, ``failed`` or ``no_op``."""

    tenant_id: UUID
    run_id: UUID
    status: str
    counts: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class WorkResult:
    model_version: str | None = None
    watermark: datetime | None = None
    counts: dict = field(default_factory=dict)
    stage_ms: dict = field(default_factory=dict)


def run_id_for(tenant_id: UUID, run_date) -> UUID:
    """Deterministic, so a re-claimed night keeps its id and the score rows that point at it."""
    return stable_uuid("run", JOB_NAME, tenant_id, run_date)


def run_nightly(
    as_of: datetime, tenant: str | UUID | None = None, trigger: str = "cli"
) -> list[TenantRun]:
    """Run the night at ``as_of`` for every tenant (or the one named by slug or UUID).

    Returns one ``TenantRun`` per tenant, in slug order. Never raises for a tenant's failure; it
    raises only for bad arguments or an unknown tenant.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}, got {trigger!r}")
    as_of = as_of.astimezone(UTC)
    return [run_tenant(tid, as_of, trigger) for tid in tenant_ids(tenant)]


def run_tenant(tenant_id: UUID, as_of: datetime, trigger: str) -> TenantRun:
    """Claim, work and finish one tenant's night. Every failure is caught and recorded."""
    run_id = run_id_for(tenant_id, as_of.date())
    try:
        claimed = claim(tenant_id, run_id, as_of, trigger)
    except Exception as exc:  # e.g. the database is unreachable: nothing could be recorded
        log.exception("nightly %s: claim failed", tenant_id)
        return TenantRun(tenant_id, run_id, "failed", error=describe(exc))
    if not claimed:
        log.info("nightly %s %s: already succeeded, no_op", tenant_id, as_of.date())
        return TenantRun(tenant_id, run_id, "no_op")

    result = WorkResult()
    try:
        with tenant_tx(tenant_id) as conn:
            do_work(conn, tenant_id, as_of, run_id, result)
        status, error = "succeeded", None
    except Exception as exc:
        log.exception("nightly %s: work failed", tenant_id)
        # The work transaction rolled back, so its counts describe nothing that was kept.
        # stage_ms is kept: the first stage missing from it is the one that failed.
        result.model_version, result.watermark, result.counts = None, None, {}
        status, error = "failed", describe(exc)

    try:
        finish(tenant_id, run_id, status, result, error)
    except Exception as exc:  # the row stays 'running'; the next run re-claims it
        log.exception("nightly %s: could not record the result", tenant_id)
        return TenantRun(tenant_id, run_id, "failed", result.counts, describe(exc))
    log.info("nightly %s %s: %s %s", tenant_id, as_of.date(), status, result.counts)
    return TenantRun(tenant_id, run_id, status, result.counts, error)


def claim(tenant_id: UUID, run_id: UUID, as_of: datetime, trigger: str) -> bool:
    """Insert or take over the night's job_runs row. False means it already succeeded."""
    with tenant_tx(tenant_id) as conn:
        row = conn.execute(
            CLAIM_SQL,
            {
                "run_id": run_id,
                "job_name": JOB_NAME,
                "tid": tenant_id,
                "run_date": as_of.date(),
                "as_of": as_of,
                "trigger": trigger,
            },
        ).one_or_none()
    return row is not None


def do_work(
    conn: Connection, tenant_id: UUID, as_of: datetime, run_id: UUID, result: WorkResult
) -> None:
    """Ingest, snapshot and score inside the caller's transaction, filling ``result`` as it goes.

    The active model is looked up first, so a missing model fails the night before any work.
    ``snapshots_written`` and ``accounts_scored`` count new rows: on a Monday ``as_of`` the
    anchor snapshot already holds that key, so the night writes no snapshot and scores the
    anchor row instead (docs/decisions/step-1-log.md, ml entry).
    """
    result.model_version, _ = active_model(conn)

    started = perf_counter()
    resolved = resolve(conn, tenant_id, as_of)
    result.watermark = ingest_watermark(conn, as_of)
    result.stage_ms["ingest"] = elapsed_ms(started)

    started = perf_counter()
    snapshots = write_snapshots(conn, tenant_id, as_of, "nightly")
    result.stage_ms["snapshot"] = elapsed_ms(started)

    started = perf_counter()
    scored = score_tenant(conn, tenant_id, as_of, run_id)
    result.stage_ms["score"] = elapsed_ms(started)

    result.counts = {
        "events_normalised": resolved.normalised,
        "events_quarantined": resolved.quarantined,
        "snapshots_written": snapshots,
        "accounts_scored": scored,
    }


def finish(
    tenant_id: UUID, run_id: UUID, status: str, result: WorkResult, error: str | None
) -> None:
    with tenant_tx(tenant_id) as conn:
        conn.execute(
            FINISH_SQL,
            {
                "run_id": run_id,
                "status": status,
                "model_version": result.model_version,
                "watermark": result.watermark,
                "counts": json.dumps(result.counts),
                "stage_ms": json.dumps(result.stage_ms),
                "error": error,
            },
        )


def elapsed_ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


def describe(exc: BaseException) -> str:
    """One line for job_runs.error: the exception type and message, capped in length."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
