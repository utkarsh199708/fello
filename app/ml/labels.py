"""Weekly training anchors and their labels.

**Anchors.** One snapshot per active account every Monday at 00:00 UTC, built by the same
``compute_features`` the nightly job uses. Weekly, not daily: with a few dozen events per account
a year, daily rows would be near-copies of each other (docs/PLAN.md section 6).

**Label.** The outcome of the account's next renewal after the anchor. The next renewal is the
``end_date`` of the contract active at the anchor, chosen by the same ``active_contracts`` the
features use: an anchor at 00:00 on a renewal date whose outcome had not yet arrived is still in
the ending cycle, so it is labelled with that renewal. The outcome is read from the Salesforce
renewal event for that date (``opportunity_stage_changed`` with ``renewal_date == end_date``):

- 1 if the renewal was lost (``closed_lost``), or if it renewed below
  ``(1 - contraction_threshold) * prior_arr`` (below 70% of prior ARR at the default 0.30);
- 0 if it renewed at or above that;
- NaN (censored, dropped from training) if no such event had been received by ``label_cutoff``.

Stripe reports the same renewal (``subscription.deleted`` or ``subscription.updated``), and
resolve stamps it with the contract's renewal_date. ``reconcile_outcomes`` makes one outcome per
renewal: Salesforce when it has reported, Stripe otherwise (task 3.1, duplicate kind 3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from app.ml.features import (
    RENEWAL_EVENT,
    RENEWAL_EVENTS,
    active_contracts,
    compute_features,
    load_feature_inputs,
    write_snapshots,
)

# The one outcome event the Step 1 labeler reads: the same event that closes a contract cycle.
LABEL_EVENT = RENEWAL_EVENT


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


def reconcile_outcomes(outcomes: pd.DataFrame, contracts: pd.DataFrame) -> pd.DataFrame:
    """One outcome per (account, renewal_date), with stage, prior_arr and renewed_arr.

    Salesforce is the system of record (docs/brief.md): its report wins when it exists. A
    renewal only Stripe reported is translated: ``subscription.deleted`` is a lost renewal, and
    ``subscription.updated`` renewed at its ``arr_usd``, against the ARR of the contract that
    ended. Pure. ``outcomes`` as from ``read_outcomes``.
    """
    columns = ["account_id", "renewal_date", "received_at", "stage", "prior_arr", "renewed_arr"]
    is_renewal = pd.MultiIndex.from_arrays([outcomes["source"], outcomes["event_type"]]).isin(
        list(RENEWAL_EVENTS)
    )
    rows = outcomes[is_renewal & outcomes["renewal_date"].notna()]
    sf = rows[(rows["source"] == LABEL_EVENT[0]) & (rows["event_type"] == LABEL_EVENT[1])]
    stripe = rows[rows["source"] == "stripe"].merge(
        contracts[["account_id", "end", "arr"]],
        left_on=["account_id", "renewal_date"],
        right_on=["account_id", "end"],
    )
    lost = stripe["event_type"] == "subscription.deleted"
    stripe = stripe.assign(
        stage=lost.map({True: "closed_lost", False: "closed_won"}),
        prior_arr=stripe["arr"].astype("float64"),
        renewed_arr=stripe.get("arr_usd", pd.Series(float("nan"), index=stripe.index))
        .where(~lost, 0.0)
        .astype("float64"),
    )
    both = pd.concat([sf[columns].assign(rank=0), stripe[columns].assign(rank=1)])
    # Within one source, the latest arrival wins (a corrected report); across sources, Salesforce.
    both = both.sort_values(["rank", "received_at"], ascending=[True, False])
    return both.drop_duplicates(["account_id", "renewal_date"]).drop(columns="rank")


def label_frame(
    anchors: pd.DataFrame,
    contracts: pd.DataFrame,
    outcomes: pd.DataFrame,
    thresholds: dict[UUID, float],
    label_cutoff: datetime,
) -> pd.DataFrame:
    """Per anchor row: ``label`` (1, 0 or NaN when censored) and ``renewal_date``, the end of
    the contract the label is about (NaT when the account has no active contract). Pure; aligned
    to ``anchors.index``. ``renewal_date`` is what the cycle-based split sorts by (task 4.2).

    - ``anchors``: tenant_id, account_id, as_of (UTC timestamps).
    - ``contracts``: account_id, cycle_number, start, end (UTC midnights), as from
      ``contracts_frame``.
    - ``outcomes``: outcome events with source, event_type, account_id, received_at, stage,
      renewal_date (UTC midnight), prior_arr, renewed_arr.
    - ``thresholds``: tenant_id -> contraction_threshold.

    The active contract of each anchor is decided with the outcomes received by that anchor's
    ``as_of`` (what the featurizer saw); the label then uses those received by ``label_cutoff``.
    """
    if anchors.empty:
        return pd.DataFrame(
            {
                "label": pd.Series(dtype="float64"),
                "renewal_date": pd.Series(dtype="datetime64[ns, UTC]"),
            },
            index=anchors.index,
        )
    rows = anchors[["tenant_id", "account_id", "as_of"]].rename_axis("row").reset_index()
    # One active_contracts call per anchor time: a few hundred Mondays, each a vector operation.
    active = pd.concat(
        group.merge(
            active_contracts(contracts, outcomes, as_of)[["end"]],
            left_on="account_id",
            right_index=True,
        )
        for as_of, group in rows.groupby("as_of")
    )

    known = reconcile_outcomes(
        outcomes[outcomes["received_at"] <= pd.Timestamp(label_cutoff)], contracts
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
    renewal = pd.Series(active["end"].to_numpy(), index=active["row"])
    return pd.DataFrame(
        {
            "label": labels.reindex(anchors.index),  # rows with no visible outcome become NaN
            "renewal_date": renewal.reindex(anchors.index),
        }
    )


def label_anchors(
    anchors: pd.DataFrame,
    contracts: pd.DataFrame,
    outcomes: pd.DataFrame,
    thresholds: dict[UUID, float],
    label_cutoff: datetime,
) -> pd.Series:
    """Label each anchor row 1, 0 or NaN (censored). Pure; aligned to ``anchors.index``."""
    return label_frame(anchors, contracts, outcomes, thresholds, label_cutoff)["label"]
