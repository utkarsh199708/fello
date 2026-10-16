"""Weekly training anchors and their labels.

**Anchors.** One snapshot per active account every Monday at 00:00 UTC, built by the same
``compute_features`` the nightly job uses. Weekly, not daily: with a few dozen events per account
a year, daily rows would be near-copies of each other (docs/PLAN.md section 6).

**Label.** The outcome of the account's next renewal after the anchor. The next renewal is the
``end_date`` of the contract active at the anchor. The outcome is read from the Salesforce
renewal event for that date (``opportunity_stage_changed`` with ``renewal_date == end_date``):

- 1 if the renewal was lost (``closed_lost``), or if it renewed below
  ``(1 - contraction_threshold) * prior_arr`` (below 70% of prior ARR at the default 0.30);
- 0 if it renewed at or above that;
- NaN (censored, dropped from training) if no such event had been received by ``label_cutoff``.

The Stripe subscription events are outcome events too, so the featurizer hides them, but the
labeler ignores them until task 3.1 reconciles the two sources (design deviation 2.6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from app.ml.features import compute_features, load_feature_inputs, write_snapshots
from app.vocab import OUTCOME_EVENT_TYPES

# The one outcome event the Step 1 labeler reads.
LABEL_EVENT = ("salesforce", "opportunity_stage_changed")
assert LABEL_EVENT in OUTCOME_EVENT_TYPES  # the featurizer must be hiding what we label from


def monday_anchors(start: datetime, end: datetime) -> list[datetime]:
    """Every Monday 00:00 UTC in ``[start, end]``, oldest first."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    if day < start:  # start is after midnight, so that day's 00:00 is out of range
        day += timedelta(days=1)
    day += timedelta(days=(7 - day.weekday()) % 7)  # weekday() is 0 on Monday
    anchors = []
    while day <= end:
        anchors.append(day)
        day += timedelta(days=7)
    return anchors


def build_anchors(conn: Connection, tenant_id: UUID | str, start: datetime, end: datetime) -> int:
    """Write anchor snapshots for every Monday in ``[start, end]``; return how many were new.

    The tenant's inputs are read once, up to ``end``. ``compute_features`` then applies each
    Monday's own visibility cut, so every anchor sees only what had arrived by its Monday. Safe to
    rerun: stored anchors are skipped by the snapshot key.
    """
    inputs = load_feature_inputs(conn, tenant_id, end)
    written = 0
    for as_of in monday_anchors(start, end):
        features = compute_features(inputs, as_of)
        written += write_snapshots(conn, tenant_id, as_of, "anchor", features)
    return written


def label_anchors(
    anchors: pd.DataFrame,
    contracts: pd.DataFrame,
    outcomes: pd.DataFrame,
    thresholds: dict[UUID, float],
    label_cutoff: datetime,
) -> pd.Series:
    """Label each anchor row 1, 0 or NaN (censored). Pure; aligned to ``anchors.index``.

    - ``anchors``: tenant_id, account_id, as_of (UTC timestamps).
    - ``contracts``: account_id, start, end (UTC midnights), as from ``contracts_frame``.
    - ``outcomes``: outcome events with source, event_type, account_id, received_at, stage,
      renewal_date (UTC midnight), prior_arr, renewed_arr.
    - ``thresholds``: tenant_id -> contraction_threshold.
    """
    rows = anchors[["tenant_id", "account_id", "as_of"]].rename_axis("row").reset_index()
    with_contract = rows.merge(contracts[["account_id", "start", "end"]], on="account_id")
    active = with_contract[
        (with_contract["start"] <= with_contract["as_of"])
        & (with_contract["as_of"] < with_contract["end"])
    ]

    known = outcomes[
        (outcomes["source"] == LABEL_EVENT[0])
        & (outcomes["event_type"] == LABEL_EVENT[1])
        & (outcomes["received_at"] <= pd.Timestamp(label_cutoff))
    ]
    # One outcome per renewal. Step 1 sends no duplicates; if one appears, the latest arrival wins.
    known = known.sort_values("received_at").drop_duplicates(
        ["account_id", "renewal_date"], keep="last"
    )
    decided = active.merge(
        known[["account_id", "renewal_date", "stage", "prior_arr", "renewed_arr"]],
        left_on=["account_id", "end"],
        right_on=["account_id", "renewal_date"],
    )
    threshold = decided["tenant_id"].map(thresholds).astype("float64")
    lost = decided["stage"] == "closed_lost"
    contracted = decided["renewed_arr"] < (1 - threshold) * decided["prior_arr"]
    labels = pd.Series((lost | contracted).astype("float64").to_numpy(), index=decided["row"])
    return labels.reindex(anchors.index)  # rows with no visible outcome become NaN
