"""Feature tests: NULL when not connected, window edges, the outcome deny-list, snapshots.

Most tests build ``FeatureInputs`` by hand and call the pure ``compute_features``, so every event
is there for a reason. The last tests run against the seeded tiny tenants.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
import sqlalchemy as sa

from app.config import load_config
from app.db import tenant_tx
from app.ingest.resolve import ingest_watermark
from app.ml.feature_spec import FEATURE_EVENTS, FEATURE_SET_VERSION, FEATURES
from app.ml.features import (
    REQUIRED_EVENT_COLUMNS,
    FeatureInputs,
    active_contracts,
    build_features,
    compute_features,
    contracts_frame,
    utc,
    write_snapshots,
)
from app.vocab import OUTCOME_EVENT_TYPES
from tests.helpers import TINY, purge_tenants, seed_tiny

AS_OF = datetime(2026, 10, 12, tzinfo=UTC)
A, B, ENDED, FUTURE = (uuid.UUID(f"00000000-0000-4000-8000-0000000000b{n}") for n in range(4))
ALL_SOURCES = {s: datetime(2024, 1, 1, tzinfo=UTC) for s in ("salesforce", "zendesk", "product")}
ALL_SOURCES |= {s: datetime(2024, 1, 1, tzinfo=UTC) for s in ("stripe", "gmail")}


def ev(account, source, event_type, occurred, received=None, active_users=None, thread_id=None):
    """One event row; received defaults to one second after it occurred."""
    received = occurred + timedelta(seconds=1) if received is None else received
    return (account, source, event_type, occurred, received, active_users, thread_id, None)


def renewal(account, renewal_date: date, received: datetime):
    """The Salesforce event that reports the outcome of the renewal on ``renewal_date``."""
    kind = ("salesforce", "opportunity_stage_changed")
    return (account, *kind, received, received, None, None, renewal_date)


def frame_of(events) -> pd.DataFrame:
    frame = pd.DataFrame(events, columns=list(REQUIRED_EVENT_COLUMNS))
    frame["occurred_at"] = utc(frame["occurred_at"])
    frame["received_at"] = utc(frame["received_at"])
    frame["active_users"] = frame["active_users"].astype("float64")
    frame["renewal_date"] = utc(frame["renewal_date"])
    return frame


# A's renewal into cycle 2 and ENDED's churn, both reported the afternoon of the renewal date.
RENEWALS = [
    renewal(A, date(2026, 1, 1), datetime(2026, 1, 1, 15, tzinfo=UTC)),
    renewal(ENDED, date(2026, 10, 5), datetime(2026, 10, 5, 15, tzinfo=UTC)),
]


def inputs(events, connected=None) -> FeatureInputs:
    """A and B are active at AS_OF. ENDED churned before it; FUTURE's contract starts after."""
    return FeatureInputs(
        connected_at=ALL_SOURCES if connected is None else connected,
        accounts=pd.DataFrame(
            {
                "account_id": [A, B, ENDED, FUTURE],
                "started_at": utc([AS_OF - timedelta(days=400)] * 3 + [AS_OF]),
            }
        ),
        contracts=contracts_frame(
            [
                (A, 1, date(2025, 1, 1), date(2026, 1, 1), 90_000),
                (A, 2, date(2026, 1, 1), date(2027, 1, 1), 100_000),
                (B, 1, date(2026, 7, 4), date(2027, 7, 4), 5_000),
                (ENDED, 1, date(2025, 10, 5), date(2026, 10, 5), 7_000),
                (FUTURE, 1, date(2026, 10, 13), date(2027, 10, 13), 8_000),
            ]
        ),
        events=frame_of(RENEWALS + list(events)),
    )


def test_population_and_contract_features():
    out = compute_features(inputs([]), AS_OF)
    assert list(out.columns) == list(FEATURES)
    assert list(out.index) == [A, B]  # ENDED's churn has arrived, FUTURE has not started
    assert out.loc[A, "arr"] == 100_000 and out.loc[A, "cycle_number"] == 2
    assert out.loc[A, "days_to_renewal"] == (date(2027, 1, 1) - date(2026, 10, 12)).days
    assert out.loc[A, "account_tenure_days"] == 400
    # Later the same day, the number of days to renewal does not change.
    later = compute_features(inputs([]), AS_OF + timedelta(hours=23))
    assert later.loc[A, "days_to_renewal"] == out.loc[A, "days_to_renewal"]


RENEWED, CHURNED = (uuid.UUID(f"00000000-0000-4000-8000-0000000000e{n}") for n in range(2))
D = date(2026, 10, 13)  # both renewals fall due on this day
D0 = datetime(2026, 10, 13, tzinfo=UTC)


def renewal_day_inputs(received: datetime) -> FeatureInputs:
    """RENEWED renews on D at a lower ARR; CHURNED is lost on D. Both outcomes arrive at received.

    contracts holds RENEWED's next cycle already, as contracts.csv does (it runs ahead of time).
    """
    return FeatureInputs(
        connected_at=ALL_SOURCES,
        accounts=pd.DataFrame(
            {"account_id": [RENEWED, CHURNED], "started_at": utc([D0 - timedelta(days=365)] * 2)}
        ),
        contracts=contracts_frame(
            [
                (RENEWED, 1, date(2025, 10, 13), D, 100_000),
                (RENEWED, 2, D, date(2027, 10, 13), 60_000),
                (CHURNED, 1, date(2025, 10, 13), D, 50_000),
            ]
        ),
        events=frame_of([renewal(RENEWED, D, received), renewal(CHURNED, D, received)]),
    )


def test_a_renewal_moves_the_account_only_once_its_outcome_has_arrived():
    data = renewal_day_inputs(received=D0 + timedelta(hours=12))
    # The nightly run at 02:00 on the renewal day: the outcomes are not in yet.
    night = compute_features(data, D0 + timedelta(hours=2))
    assert list(night.index) == sorted([RENEWED, CHURNED])  # the churned account is still listed
    assert night.loc[RENEWED, "arr"] == 100_000 and night.loc[RENEWED, "cycle_number"] == 1
    assert (night["days_to_renewal"] == 0).all()
    # From the moment the outcome arrives, and the next night, the account has moved.
    for as_of in (D0 + timedelta(hours=12), D0 + timedelta(days=1, hours=2)):
        out = compute_features(data, as_of)
        assert list(out.index) == [RENEWED]
        assert out.loc[RENEWED, "arr"] == 60_000 and out.loc[RENEWED, "cycle_number"] == 2
    assert out.loc[RENEWED, "days_to_renewal"] == 364
    # An outcome that is late by days keeps the account in its old cycle, overdue.
    late = renewal_day_inputs(received=D0 + timedelta(days=3, hours=1))
    out = compute_features(late, D0 + timedelta(days=3))
    assert list(out.index) == sorted([RENEWED, CHURNED])
    assert (out["days_to_renewal"] == -3).all() and (out["cycle_number"] == 1).all()


def test_active_contracts_ignores_other_events_and_early_outcomes():
    data = renewal_day_inputs(received=D0 - timedelta(days=2))  # closed two days before D
    before = active_contracts(data.contracts, data.events, D0 - timedelta(seconds=1))
    assert before["cycle_number"].to_dict() == {RENEWED: 1, CHURNED: 1}  # D has not come yet
    on_the_day = active_contracts(data.contracts, data.events, D0)
    assert on_the_day["cycle_number"].to_dict() == {RENEWED: 2}
    # A renewal_date on any other event type moves nothing.
    other = data.events.assign(event_type="account_upserted")
    assert len(active_contracts(data.contracts, other, D0 + timedelta(days=1))) == 2


def test_window_edges_and_visibility():
    day = timedelta(days=1)
    events = [
        ev(A, "zendesk", "ticket_created", AS_OF - 30 * day),  # on the edge: prior window
        ev(A, "zendesk", "ticket_created", AS_OF - 30 * day + timedelta(seconds=1)),  # inside
        ev(A, "zendesk", "ticket_created", AS_OF, received=AS_OF),  # at as_of: inside
        ev(A, "zendesk", "ticket_created", AS_OF - 2 * day, received=AS_OF + timedelta(seconds=1)),
        ev(A, "zendesk", "ticket_created", AS_OF - 120 * day),  # past the prior window
        ev(A, "zendesk", "ticket_updated", AS_OF - 1 * day),  # an update is not a new ticket
        ev(A, "stripe", "invoice.payment_failed", AS_OF - 90 * day),  # edge: outside
        ev(A, "stripe", "invoice.payment_failed", AS_OF - 89 * day),
    ]
    out = compute_features(inputs(events), AS_OF)
    assert out.loc[A, "tickets_30d"] == 2
    assert out.loc[A, "ticket_rate_30d_vs_prior_90d"] == pytest.approx((2 + 1) / (1 / 3 + 1))
    assert out.loc[A, "failed_payments_90d"] == 1
    assert out.loc[A, "total_event_count"] == 7  # everything visible, whatever its age
    assert out.loc[B, "tickets_30d"] == 0 and out.loc[B, "total_event_count"] == 0
    # One second later the late ticket has arrived and counts, and the window has moved past the
    # ticket that was one second inside it.
    later = compute_features(inputs(events), AS_OF + timedelta(seconds=1))
    assert later.loc[A, "total_event_count"] == 8
    assert later.loc[A, "tickets_30d"] == 2


def test_not_connected_is_null_and_connected_without_events_is_zero():
    connected = {
        "salesforce": AS_OF - timedelta(days=500),
        "zendesk": AS_OF - timedelta(days=500),
        "product": AS_OF - timedelta(days=500),
        "stripe": AS_OF + timedelta(days=1),  # connected after as_of
        # gmail never connected
    }
    out = compute_features(inputs([], connected), AS_OF)
    assert out["failed_payments_90d"].isna().all() and out["threads_90d"].isna().all()
    assert (out["stripe_connected"] == 0).all() and (out["gmail_connected"] == 0).all()
    assert (out["tickets_30d"] == 0).all() and (out["zendesk_connected"] == 1).all()
    for flag in ("zendesk_connected", "product_connected", "stripe_connected", "gmail_connected"):
        assert out[flag].notna().all()


def test_usage_features():
    month = timedelta(days=30)
    newest = AS_OF - timedelta(days=11)
    events = [
        ev(A, "product", "usage_rollup_monthly", newest, active_users=50),
        *(
            ev(A, "product", "usage_rollup_monthly", newest - n * month, active_users=100)
            for n in (1, 2, 3)
        ),
        ev(A, "product", "usage_rollup_monthly", newest - 4 * month, active_users=1),  # too old
        # B's newest report is 60 days old: last month is unknown, not zero.
        ev(B, "product", "usage_rollup_monthly", AS_OF - timedelta(days=60), active_users=10),
    ]
    out = compute_features(inputs(events), AS_OF)
    assert out.loc[A, "active_users_last_month"] == 50
    assert out.loc[A, "usage_change_last_month_vs_prior_3"] == pytest.approx(-0.5)
    assert pd.isna(out.loc[B, "active_users_last_month"])
    assert pd.isna(out.loc[B, "usage_change_last_month_vs_prior_3"])


def test_threads_count_distinct_threads():
    events = [
        ev(A, "gmail", "message", AS_OF - timedelta(days=d), thread_id=t)
        for d, t in ((1, "th1"), (2, "th1"), (3, "th2"), (100, "th3"))
    ]
    assert compute_features(inputs(events), AS_OF).loc[A, "threads_90d"] == 2


def test_adding_an_outcome_event_changes_no_feature():
    day = timedelta(days=1)
    base = [
        ev(A, "zendesk", "ticket_created", AS_OF - 3 * day),
        ev(A, "stripe", "invoice.paid", AS_OF - 3 * day),
        ev(B, "salesforce", "account_upserted", AS_OF - 3 * day),
    ]
    outcomes = [
        ev(account, source, event_type, AS_OF - 2 * day)
        for account in (A, B)
        for source, event_type in sorted(OUTCOME_EVENT_TYPES)
    ]
    before = compute_features(inputs(base), AS_OF)
    after = compute_features(inputs(base + outcomes), AS_OF)
    pd.testing.assert_frame_equal(before, after)


def test_feature_events_name_every_feature_and_no_outcome_type():
    assert tuple(FEATURE_EVENTS) == FEATURES
    for spec in FEATURE_EVENTS.values():
        if spec is not None:
            for source in spec.sources:
                for event_type in spec.event_types:
                    assert (source, event_type) not in OUTCOME_EVENT_TYPES
    assert not FEATURE_EVENTS["total_event_count"].matches(
        "salesforce", "opportunity_stage_changed"
    )
    assert FEATURE_EVENTS["tickets_30d"].matches("zendesk", "ticket_created")


def test_feature_spec_imports_nothing_heavy():
    code = (
        "import sys, app.ml.feature_spec; "
        "heavy = {'pandas', 'numpy', 'sklearn'} & set(sys.modules); "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_naive_as_of_is_refused():
    with pytest.raises(ValueError):
        compute_features(inputs([]), datetime(2026, 10, 12))


# --- against the database ------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    """Seed the tiny tenants; purge them afterwards, since tenant slugs are unique per database."""
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def test_stored_snapshot_equals_a_recompute(seeded):
    as_of = load_config(TINY).train_as_of
    tid = seeded[0]
    with tenant_tx(tid) as conn:
        stored = conn.execute(
            sa.text(
                "SELECT account_id, features, ingest_watermark, feature_set_version, built_at"
                " FROM snapshots WHERE as_of = :as_of ORDER BY account_id"
            ),
            {"as_of": as_of},
        ).all()
        fresh = build_features(conn, tid, as_of)
        watermark = ingest_watermark(conn, as_of)
        assert write_snapshots(conn, tid, as_of, "nightly") == 0  # a rerun writes nothing
    assert len(stored) == len(fresh) > 0
    for row in stored:
        assert row.feature_set_version == FEATURE_SET_VERSION
        assert row.ingest_watermark == watermark and row.built_at is not None
        expected = fresh.loc[row.account_id]
        for name in FEATURES:
            value = row.features[name]
            if value is None:
                assert pd.isna(expected[name])
            else:
                assert value == pytest.approx(expected[name], rel=1e-12)


def test_tenant_without_gmail_has_null_gmail_features(seeded):
    with tenant_tx(seeded[1]) as conn:  # the second tiny tenant has no Gmail
        rows = conn.execute(sa.text("SELECT features FROM snapshots")).scalars().all()
    assert rows
    assert all(f["threads_90d"] is None and f["gmail_connected"] == 0 for f in rows)
    assert all(f["tickets_30d"] is not None for f in rows)
