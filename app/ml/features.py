"""Point-in-time features: one row per active account, as the account looked at ``as_of``.

The same ``compute_features`` builds the weekly training anchors and the nightly snapshots, so a
model is trained on exactly the kind of row it later scores. The rules (visibility, windows,
outcome deny-list, NULL when not connected) are listed in ``app/ml/feature_spec.py``.

Shape of the work:

- ``load_feature_inputs`` reads everything one tenant had received by ``upto`` with plain SQL.
- ``compute_features`` is pure: given those inputs and an ``as_of`` it returns the feature table.
  It reads no clock and no database, which is what makes a snapshot reproducible.
- ``write_snapshots`` stores the table, keyed so that a rerun at the same ``as_of`` writes nothing.

Functions that take ``conn`` never open or commit a transaction: the caller wraps them in
``tenant_tx`` (design section 1).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import parse_utc
from app.db import copy_insert
from app.ids import stable_uuid
from app.ingest.resolve import ingest_watermark
from app.ml.feature_spec import (
    CONNECTED_FLAGS,
    FEATURE_SET_VERSION,
    FEATURE_SOURCE,
    FEATURES,
    PAYMENT_WINDOW,
    THREAD_WINDOW,
    TICKET_PRIOR_WINDOW,
    TICKET_WINDOW,
    USAGE_HISTORY_WINDOW,
    USAGE_RECENT_WINDOW,
)
from app.vocab import OUTCOME_EVENT_TYPES

SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "tenant_id",
    "account_id",
    "as_of",
    "kind",
    "feature_set_version",
    "features",
    "ingest_watermark",
)

# Columns of FeatureInputs.events. Three payload fields are pulled out: two features need
# active_users and thread_id, and active_contracts needs renewal_date. Everything else in attrs
# stays in the database.
EVENT_COLUMNS = (
    "account_id",
    "source",
    "event_type",
    "occurred_at",
    "received_at",
    "active_users",
    "thread_id",
    "renewal_date",
)

# The Salesforce event that reports how a renewal ended. It is an outcome event, so no feature
# counts it, but its arrival is what moves an account from one contract cycle to the next.
RENEWAL_EVENT = ("salesforce", "opportunity_stage_changed")
# Every event that reports a renewal: Salesforce, and Stripe where the tenant has it. Resolve
# stamps the Stripe ones with the renewal_date of the contract they renew. The two sources are
# reconciled into one decision per (account, renewal_date) by ``renewal_decisions``.
RENEWAL_EVENTS = frozenset(
    {
        RENEWAL_EVENT,
        ("stripe", "subscription.updated"),
        ("stripe", "subscription.deleted"),
    }
)
assert RENEWAL_EVENTS <= OUTCOME_EVENT_TYPES


def renewal_decisions(events: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """One row per (account, renewal_date): the first report of that renewal from any source.

    ``events`` needs account_id, source, event_type, received_at and renewal_date. Rows of other
    types, or without a renewal_date (a Stripe event that matched no contract), are ignored.
    With ``as_of``, only reports received by then count. Duplicate kind 3: Salesforce and Stripe
    report the same renewal, and it is decided once, when the first of them arrives.
    """
    is_renewal = pd.MultiIndex.from_arrays([events["source"], events["event_type"]]).isin(
        list(RENEWAL_EVENTS)
    )
    seen = events[is_renewal & events["renewal_date"].notna()]
    if as_of is not None:
        seen = seen[seen["received_at"] <= pd.Timestamp(as_of)]
    return seen.sort_values("received_at").drop_duplicates(["account_id", "renewal_date"])


@dataclass(frozen=True)
class FeatureInputs:
    """Everything one tenant had received by some time, ready for ``compute_features``.

    ``contracts.start`` and ``contracts.end`` are the contract dates as UTC midnights, so they
    compare directly with an ``as_of`` timestamp. ``events.renewal_date`` is one too (NaT on
    every event that is not a renewal).
    """

    connected_at: dict[str, datetime]  # source -> when the tenant connected it
    accounts: pd.DataFrame  # account_id, started_at
    contracts: pd.DataFrame  # account_id, cycle_number, start, end, arr
    events: pd.DataFrame  # EVENT_COLUMNS


def utc(values) -> pd.Series:
    """Timestamps or dates as one dtype (UTC, microseconds), so comparisons and merges agree."""
    return pd.to_datetime(pd.Series(values, dtype=object), utc=True).dt.as_unit("us")


def contracts_frame(rows) -> pd.DataFrame:
    """Contract rows (account_id, cycle_number, start_date, end_date, arr) as a typed frame."""
    df = pd.DataFrame(
        list(rows), columns=["account_id", "cycle_number", "start_date", "end_date", "arr"]
    )
    return pd.DataFrame(
        {
            "account_id": df["account_id"].astype(object),
            "cycle_number": df["cycle_number"].astype("int64"),
            "start": utc(df["start_date"]),
            "end": utc(df["end_date"]),
            "arr": df["arr"].astype("float64"),  # numeric arrives as Decimal
        }
    )


def load_feature_inputs(conn: Connection, tenant_id: UUID | str, upto: datetime) -> FeatureInputs:
    """Read the tenant's accounts, contracts and the events received by ``upto``.

    Outcome events are read too and dropped inside ``compute_features``, so the deny-list lives in
    one place and a test can feed it an outcome event directly.
    """
    params = {"tid": UUID(str(tenant_id)), "upto": upto}
    connected = conn.execute(
        sa.text("SELECT connected_at FROM tenants WHERE tenant_id = :tid"), params
    ).scalar_one()
    accounts = pd.DataFrame(
        conn.execute(
            sa.text("SELECT account_id, started_at FROM accounts WHERE tenant_id = :tid"), params
        ).all(),
        columns=["account_id", "started_at"],
    )
    accounts["started_at"] = utc(accounts["started_at"])
    contracts = contracts_frame(
        conn.execute(
            sa.text(
                "SELECT account_id, cycle_number, start_date, end_date, arr"
                " FROM contracts WHERE tenant_id = :tid"
            ),
            params,
        ).all()
    )
    events = pd.DataFrame(
        conn.execute(
            sa.text(
                """
                SELECT account_id, source, event_type, occurred_at, received_at,
                       (attrs->>'active_users')::int AS active_users,
                       attrs->>'thread_id' AS thread_id,
                       (attrs->>'renewal_date')::date AS renewal_date
                FROM normalised_events
                WHERE tenant_id = :tid AND received_at <= :upto
                """
            ),
            params,
        ).all(),
        columns=list(EVENT_COLUMNS),
    )
    events["occurred_at"] = utc(events["occurred_at"])
    events["received_at"] = utc(events["received_at"])
    events["active_users"] = events["active_users"].astype("float64")
    events["renewal_date"] = utc(events["renewal_date"])
    return FeatureInputs(
        connected_at={source: parse_utc(ts) for source, ts in connected.items()},
        accounts=accounts,
        contracts=contracts,
        events=events,
    )


def is_connected(connected_at: dict[str, datetime], source: str, as_of: datetime) -> bool:
    """True when the tenant had connected ``source`` by ``as_of``."""
    when = connected_at.get(source)
    return when is not None and when <= as_of


def active_contracts(contracts: pd.DataFrame, events: pd.DataFrame, as_of) -> pd.DataFrame:
    """The contract each account is in at ``as_of``, as far as the system could know then.

    contracts.csv is a CRM stand-in that already holds every future cycle. Read by dates alone,
    it would move an account to its next cycle and new ARR (or drop a churned one) at 00:00 on
    the renewal date, 9 to 17 hours before the Salesforce event that reports the outcome
    arrives. So a renewal counts as decided only once it was reported (``renewal_decisions``):

    - cycle 1 is eligible from its start date; a later cycle only once the renewal that opened
      it (``renewal_date == start``) is known;
    - a cycle whose end date has passed stays active until the renewal that closes it is known,
      so while the outcome is pending its ``days_to_renewal`` is 0, then negative.

    ``contracts`` as from ``contracts_frame``. ``events`` needs account_id, source, event_type,
    received_at and renewal_date (UTC midnight); rows of other types are ignored, so both the
    feature events and the labeler's outcome frame can be passed. Returns the active contract
    rows indexed by account_id, sorted. Used by ``compute_features`` and ``label_anchors``, so
    a training row and its label agree on which renewal is next.
    """
    t = pd.Timestamp(as_of)
    seen = renewal_decisions(events, t)
    known = pd.MultiIndex.from_arrays([seen["account_id"], seen["renewal_date"]])
    c = contracts[contracts["start"] <= t]
    opened = (c["cycle_number"] == 1) | pd.MultiIndex.from_arrays(
        [c["account_id"], c["start"]]
    ).isin(known)
    closed = (c["end"] <= t) & pd.MultiIndex.from_arrays([c["account_id"], c["end"]]).isin(known)
    # One open cycle per account is normal. If an outcome never arrives, the cycles after it are
    # never opened, so keeping the latest open one is still correct.
    open_cycles = c[opened & ~closed].sort_values("cycle_number")
    return (
        open_cycles.drop_duplicates("account_id", keep="last").set_index("account_id").sort_index()
    )


def compute_features(inputs: FeatureInputs, as_of: datetime) -> pd.DataFrame:
    """The feature table at ``as_of``: indexed by account_id, columns in ``FEATURES`` order.

    Pure. The population is the accounts with an active contract as ``active_contracts``
    defines it (renewals count once their event arrived); contract values come from that
    contract only.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware (UTC)")
    t = pd.Timestamp(as_of).tz_convert("UTC")

    active = active_contracts(inputs.contracts, inputs.events, t)
    ids = active.index
    out = pd.DataFrame(index=ids, columns=list(FEATURES), dtype="float64")
    out.index.name = "account_id"

    started = inputs.accounts.set_index("account_id")["started_at"].reindex(ids)
    out["account_tenure_days"] = (t - started).dt.days
    out["arr"] = active["arr"]
    # Whole days from as_of's date to the renewal date, so 00:00 and 23:59 on one day agree.
    # 0 or negative while a renewal that is due has no outcome yet.
    out["days_to_renewal"] = (active["end"] - t.normalize()).dt.days
    out["cycle_number"] = active["cycle_number"]

    # Events of accounts outside the population are not filtered here: every result below is
    # reindexed to the population, which drops them, and that is cheaper than isin on UUIDs.
    ev = inputs.events
    ev = ev[(ev["received_at"] <= t) & (ev["occurred_at"] <= t)]
    is_outcome = pd.MultiIndex.from_arrays([ev["source"], ev["event_type"]]).isin(
        list(OUTCOME_EVENT_TYPES)
    )
    ev = ev[~is_outcome]

    def since(days: int) -> pd.Series:
        """occurred_at inside (as_of - days, as_of]. The upper edge is already applied."""
        return ev["occurred_at"] > t - pd.Timedelta(days=days)

    def is_type(source: str, event_type: str) -> pd.Series:
        return (ev["source"] == source) & (ev["event_type"] == event_type)

    def count(mask: pd.Series) -> pd.Series:
        return ev[mask].groupby("account_id").size().reindex(ids, fill_value=0)

    out["total_event_count"] = count(pd.Series(True, index=ev.index))

    created = is_type("zendesk", "ticket_created")
    recent = count(created & since(TICKET_WINDOW))
    prior = count(created & since(TICKET_WINDOW + TICKET_PRIOR_WINDOW) & ~since(TICKET_WINDOW))
    out["tickets_30d"] = recent
    # Last 30 days against the monthly pace of the 90 days before. +1 on both sides keeps it
    # defined for quiet accounts: 1.0 means "same pace as before", 3.0 "three times the pace".
    out["ticket_rate_30d_vs_prior_90d"] = (recent + 1) / (prior / 3 + 1)

    rollups = ev[is_type("product", "usage_rollup_monthly") & since(USAGE_HISTORY_WINDOW)]
    rollups = rollups.sort_values(
        ["account_id", "occurred_at", "received_at"], ascending=[True, False, False]
    )
    position = rollups.groupby("account_id").cumcount()  # 0 is the newest report
    fresh = rollups["occurred_at"] > t - pd.Timedelta(days=USAGE_RECENT_WINDOW)
    last = rollups[(position == 0) & fresh].set_index("account_id")["active_users"].reindex(ids)
    baseline = (
        rollups[(position >= 1) & (position <= 3)]
        .groupby("account_id")["active_users"]
        .mean()
        .reindex(ids)
    )
    # A missing report is unknown, not zero users, so these stay NULL when a report is missing.
    out["active_users_last_month"] = last
    out["usage_change_last_month_vs_prior_3"] = ((last - baseline) / baseline).where(baseline > 0)

    out["failed_payments_90d"] = count(
        is_type("stripe", "invoice.payment_failed") & since(PAYMENT_WINDOW)
    )
    mail = ev[is_type("gmail", "message") & since(THREAD_WINDOW)]
    out["threads_90d"] = (
        mail.groupby("account_id")["thread_id"].nunique().reindex(ids, fill_value=0)
    )

    for flag, source in CONNECTED_FLAGS.items():
        out[flag] = 1.0 if is_connected(inputs.connected_at, source, as_of) else 0.0
    for feature, source in FEATURE_SOURCE.items():
        if not is_connected(inputs.connected_at, source, as_of):
            out[feature] = float("nan")
    return out.astype("float64")


def build_features(conn: Connection, tenant_id: UUID | str, as_of: datetime) -> pd.DataFrame:
    """Load what the tenant had received by ``as_of`` and compute the features at ``as_of``."""
    return compute_features(load_feature_inputs(conn, tenant_id, as_of), as_of)


def feature_json(values: list[float]) -> str:
    """One feature row (in FEATURES order) as JSON text.

    NaN becomes null, and whole numbers are written as integers so a count reads 3, not 3.0.
    """
    doc = {
        name: None if math.isnan(v) else (int(v) if v.is_integer() else v)
        for name, v in zip(FEATURES, values, strict=True)
    }
    return json.dumps(doc, separators=(",", ":"))


def snapshot_id(tenant_id: UUID | str, account_id: UUID | str, as_of: datetime) -> UUID:
    return stable_uuid("snapshot", tenant_id, account_id, as_of.isoformat(), FEATURE_SET_VERSION)


def write_snapshots(
    conn: Connection,
    tenant_id: UUID | str,
    as_of: datetime,
    kind: str,
    features: pd.DataFrame | None = None,
) -> int:
    """Store one snapshot row per account at ``as_of`` and return how many were new.

    ``kind`` is ``anchor`` (weekly training row) or ``nightly``. ``features`` defaults to
    ``build_features`` at ``as_of``. ``ingest_watermark`` records the newest arrival that was
    visible, so anyone can later list exactly the events the row was built from.
    ``ON CONFLICT DO NOTHING``: a snapshot, once stored, is never restated.
    """
    tid = UUID(str(tenant_id))
    if features is None:
        features = build_features(conn, tid, as_of)
    watermark = ingest_watermark(conn, as_of)
    rows = (
        (
            snapshot_id(tid, account_id, as_of),
            tid,
            account_id,
            as_of,
            kind,
            FEATURE_SET_VERSION,
            feature_json(values),
            watermark,
        )
        for account_id, values in zip(
            features.index, features[list(FEATURES)].to_numpy().tolist(), strict=True
        )
    )
    return copy_insert(
        conn, "snapshots", SNAPSHOT_COLUMNS, rows, on_conflict="ON CONFLICT DO NOTHING"
    )
