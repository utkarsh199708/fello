"""The data-quality gate: the night publishes no scores unless the data looks sane (task 3.6).

Run after scoring, inside the night's transaction. If a check trips, the night raises
``GateTripped``. The transaction rolls back, so no snapshot or score of that night is kept, and
the job_runs row says ``blocked`` with the reason. The list keeps showing the previous night, and
the raw events stay in raw_events for the next run.

Checks (PLAN.md section 7, stage 2):

- **duplicates**: no two normalised rows share a source event id. The unique index already makes
  this impossible, so this documents and asserts the invariant rather than enforcing it.
- **quarantine_rate**: of the events resolved tonight, under ``MAX_QUARANTINE_RATE`` went to
  quarantine. A sudden jump means an identity mapping broke (a connector lost its back-reference
  field), and scores would silently miss that source.
- **null_rates**: in tonight's scored feature vectors, a feature whose source is connected is
  NULL for at most ``MAX_NULL_RATE`` of accounts, and a feature whose source is not connected is
  NULL for all of them (NULL, never zero). Account and contract features are never NULL.
- **mean_shift**: the mean probability moved less than ``MAX_MEAN_SHIFT`` from the previous
  succeeded night scored by the same model. A different model (a rebaseline) skips the check,
  since a new model is expected to move the mean.

The thresholds come from the full config's measured values. The worst null rate of a connected
source's feature is 0.20 (t5's usage change), and quarantine runs at 0.4 to 1.5%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import parse_utc
from app.ml.feature_spec import CONNECTED_FLAGS, FEATURE_SOURCE, FEATURES

MAX_QUARANTINE_RATE = 0.10
MAX_NULL_RATE = 0.50
MAX_MEAN_SHIFT = 0.10


@dataclass
class GateResult:
    checks: dict[str, Any] = field(default_factory=dict)  # every measured value, stored on the run
    failed: list[str] = field(default_factory=list)  # one line per tripped check

    @property
    def passed(self) -> bool:
        return not self.failed


class GateTripped(Exception):
    def __init__(self, result: GateResult):
        super().__init__("; ".join(result.failed))
        self.result = result


def evaluate(
    *,
    duplicates: int,
    resolved: int,
    quarantined: int,
    null_rates: dict[str, float],
    connected: dict[str, bool],
    mean_probability: float | None,
    previous_mean: float | None,
) -> GateResult:
    """Pure: the gate's verdict on measured values. Inputs are what ``run_checks`` reads."""
    r = GateResult()
    r.checks["duplicates"] = duplicates
    if duplicates:
        r.failed.append(f"duplicates: {duplicates} source events normalised more than once")

    rate = quarantined / resolved if resolved else None
    r.checks["quarantine_rate"] = rate
    if rate is not None and rate >= MAX_QUARANTINE_RATE:
        r.failed.append(
            f"quarantine_rate: {rate:.1%} of tonight's events (limit {MAX_QUARANTINE_RATE:.0%})"
        )

    r.checks["null_rates"] = {k: round(v, 4) for k, v in null_rates.items() if v}
    for feature, rate in null_rates.items():
        source = FEATURE_SOURCE.get(feature)
        if source is None:
            if rate > 0:
                r.failed.append(f"null_rates: {feature} is NULL for {rate:.1%}, expected never")
        elif not connected.get(source, False):
            if rate < 1:
                r.failed.append(f"null_rates: {feature} has values but {source} is not connected")
        elif rate > MAX_NULL_RATE:
            r.failed.append(
                f"null_rates: {feature} is NULL for {rate:.1%} (limit {MAX_NULL_RATE:.0%})"
            )

    r.checks["mean_probability"] = mean_probability
    r.checks["previous_mean_probability"] = previous_mean
    if mean_probability is not None and previous_mean is not None:
        shift = abs(mean_probability - previous_mean)
        r.checks["mean_shift"] = round(shift, 4)
        if shift >= MAX_MEAN_SHIFT:
            r.failed.append(
                f"mean_shift: mean probability moved {shift:.3f} since the last night"
                f" (limit {MAX_MEAN_SHIFT})"
            )
    else:
        r.checks["mean_shift"] = "skipped: no earlier night scored by this model"
    return r


def run_checks(
    conn: Connection,
    tenant_id: UUID,
    as_of: datetime,
    run_id: UUID,
    resolved: int,
    quarantined: int,
    model_version: str | None,
) -> GateResult:
    """Measure tonight's values in the night's transaction, then ``evaluate`` them."""
    duplicates = conn.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM normalised_events"
            " GROUP BY source, source_event_id HAVING count(*) > 1) d"
        )
    ).scalar_one()

    rows = conn.execute(
        sa.text(
            "SELECT s.features, sc.probability FROM scores sc"
            " JOIN snapshots s ON s.snapshot_id = sc.snapshot_id WHERE sc.run_id = :run_id"
        ),
        {"run_id": run_id},
    ).all()
    null_rates = (
        {f: sum(row.features.get(f) is None for row in rows) / len(rows) for f in FEATURES}
        if rows
        else {}
    )
    mean = sum(row.probability for row in rows) / len(rows) if rows else None

    connected_at = conn.execute(
        sa.text("SELECT connected_at FROM tenants WHERE tenant_id = :tid"), {"tid": tenant_id}
    ).scalar_one()
    connected = {
        source: source in connected_at and parse_utc(connected_at[source]) <= as_of
        for source in CONNECTED_FLAGS.values()
    }
    previous = conn.execute(
        sa.text(
            "SELECT (counts->>'mean_probability')::float8 FROM job_runs"
            " WHERE job_name = 'nightly' AND status = 'succeeded' AND run_date < :day"
            " AND model_version = :mv AND counts ? 'mean_probability'"
            " ORDER BY run_date DESC LIMIT 1"
        ),
        {"day": as_of.date(), "mv": model_version},
    ).scalar_one_or_none()
    return evaluate(
        duplicates=duplicates,
        resolved=resolved,
        quarantined=quarantined,
        null_rates=null_rates,
        connected=connected,
        mean_probability=mean,
        previous_mean=previous,
    )
