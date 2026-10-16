"""Label and anchor tests: Mondays at 00:00 UTC, the label rules and censoring."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
import sqlalchemy as sa

from app.db import tenant_tx
from app.ml.features import contracts_frame, utc
from app.ml.labels import label_anchors, monday_anchors
from tests.helpers import purge_tenants, seed_tiny

T1 = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
T2 = uuid.UUID("00000000-0000-4000-8000-0000000000c2")
CUTOFF = datetime(2026, 10, 12, tzinfo=UTC)
ANCHOR = datetime(2026, 3, 2, tzinfo=UTC)
RENEWAL = date(2026, 6, 1)


def test_monday_anchors():
    anchors = monday_anchors(
        datetime(2026, 10, 7, 10, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC)
    )
    assert anchors == [datetime(2026, 10, d, tzinfo=UTC) for d in (12, 19, 26)]
    assert all(a.weekday() == 0 and a.time() == datetime.min.time() for a in anchors)
    assert monday_anchors(datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC))
    one_second_late = datetime(2026, 10, 12, 0, 0, 1, tzinfo=UTC)
    assert monday_anchors(one_second_late, datetime(2026, 10, 25, tzinfo=UTC)) == [
        datetime(2026, 10, 19, tzinfo=UTC)
    ]
    # A naive datetime is ambiguous, so it is refused.
    with pytest.raises(ValueError):
        monday_anchors(datetime(2026, 10, 12), datetime(2026, 10, 19))


def outcome(account, stage="closed_won", prior=100.0, renewed=100.0, renewal=RENEWAL, **kw):
    received = kw.get("received", datetime(2026, 6, 1, 12, tzinfo=UTC))
    source, event_type = kw.get("kind", ("salesforce", "opportunity_stage_changed"))
    arr_usd = kw.get("arr_usd")
    return (account, source, event_type, received, stage, renewal, prior, renewed, arr_usd)


def label_one(outcomes, tenant=T1, as_of=ANCHOR, thresholds=None):
    """Label a single anchor of account A whose active contract renews on RENEWAL."""
    a = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
    anchors = pd.DataFrame({"tenant_id": [tenant], "account_id": [a], "as_of": utc([as_of])})
    contracts = contracts_frame(
        [
            (a, 1, date(2025, 6, 1), RENEWAL, 100),
            (a, 2, RENEWAL, date(2027, 6, 1), 100),
        ]
    )
    columns = [
        "account_id",
        "source",
        "event_type",
        "received_at",
        "stage",
        "renewal_date",
        "prior_arr",
        "renewed_arr",
        "arr_usd",
    ]
    frame = pd.DataFrame([outcome(a, **o) for o in outcomes], columns=columns)
    frame["received_at"] = utc(frame["received_at"])
    frame["renewal_date"] = utc(frame["renewal_date"])
    thresholds = thresholds or {T1: 0.30, T2: 0.50}
    return label_anchors(anchors, contracts, frame, thresholds, CUTOFF).iloc[0]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([{"stage": "closed_lost", "renewed": 0.0}], 1.0),
        ([{"renewed": 69.0}], 1.0),  # contracted below 70% of prior ARR
        ([{"renewed": 70.0}], 0.0),  # exactly 70% is not below it
        ([{"renewed": 105.0}], 0.0),
    ],
)
def test_label_rules(outcomes, expected):
    assert label_one(outcomes) == expected


def test_threshold_is_per_tenant():
    assert label_one([{"renewed": 60.0}], tenant=T2) == 0.0  # T2 tolerates down to 50%
    assert label_one([{"renewed": 60.0}], tenant=T1) == 1.0


@pytest.mark.parametrize(
    "outcomes",
    [
        [],  # renewal not decided yet
        [{"received": CUTOFF + timedelta(seconds=1)}],  # decided, but arrived after the cutoff
        [{"renewal": date(2025, 6, 1)}],  # an outcome for another renewal
        # Stripe reported, but resolve could not tie it to a contract: no renewal_date.
        [{"kind": ("stripe", "subscription.deleted"), "renewal": None}],
    ],
)
def test_censored_rows_are_nan(outcomes):
    assert pd.isna(label_one(outcomes))


STRIPE_DELETED = ("stripe", "subscription.deleted")
STRIPE_UPDATED = ("stripe", "subscription.updated")


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        # Only Stripe reported the renewal: its events are translated (task 3.1).
        ([{"kind": STRIPE_DELETED, "stage": None, "prior": None, "renewed": None}], 1.0),
        ([{"kind": STRIPE_UPDATED, "stage": None, "prior": None, "arr_usd": 60.0}], 1.0),
        ([{"kind": STRIPE_UPDATED, "stage": None, "prior": None, "arr_usd": 95.0}], 0.0),
        # Both reported: one outcome, and Salesforce, the system of record, wins.
        ([{}, {"kind": STRIPE_DELETED, "stage": None}], 0.0),
        (
            [{"stage": "closed_lost", "renewed": 0.0}, {"kind": STRIPE_UPDATED, "arr_usd": 100.0}],
            1.0,
        ),
    ],
)
def test_salesforce_and_stripe_reconcile_to_one_outcome(outcomes, expected):
    assert label_one(outcomes) == expected


def test_the_label_is_the_next_renewal_after_the_anchor():
    # From the anchor after the outcome arrived, the active contract is cycle 2, renewing in
    # 2027: the 2026 outcome no longer labels it.
    after = datetime(2026, 6, 8, tzinfo=UTC)
    assert pd.isna(label_one([{"renewed": 50.0}], as_of=after))
    # An anchor at 00:00 on the renewal day comes before the outcome (received at 12:00), so the
    # account is still in cycle 1, as its features say, and that renewal is its label.
    on_the_day = datetime(2026, 6, 1, tzinfo=UTC)
    assert label_one([{"renewed": 50.0}], as_of=on_the_day) == 1.0
    # Had the outcome arrived by then, the anchor would already belong to cycle 2.
    early = {"renewed": 50.0, "received": datetime(2026, 5, 31, 12, tzinfo=UTC)}
    assert pd.isna(label_one([early], as_of=on_the_day))


def test_no_active_contract_is_nan():
    assert pd.isna(label_one([{"stage": "closed_lost"}], as_of=datetime(2024, 1, 1, tzinfo=UTC)))


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    """Seed the tiny tenants; purge them afterwards, since tenant slugs are unique per database."""
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def test_stored_anchors_fall_on_monday_midnight_utc(seeded):
    with tenant_tx(seeded[0]) as conn:
        anchors = (
            conn.execute(sa.text("SELECT DISTINCT as_of FROM snapshots WHERE kind = 'anchor'"))
            .scalars()
            .all()
        )
    assert len(anchors) > 50
    assert all(a.weekday() == 0 and a.time() == datetime.min.time() for a in anchors)
    assert all(a.utcoffset() == timedelta(0) for a in anchors)
