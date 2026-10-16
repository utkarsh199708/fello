"""A late event leaves the stored snapshot untouched and changes the next one.

A "late" event occurred before snapshot D but was received after it (``occurred_at <= D <
received_at``). The point-in-time rule says snapshot D could not have known it, so:

- the stored snapshot D is unchanged, and recomputing D after the event is resolved still gives
  the stored values (visibility is ``received_at <= as_of``, not ``occurred_at <= as_of``);
- the next snapshot D+1, which receives it, differs, and only for the event's account.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.db import tenant_tx
from app.ingest.resolve import resolve
from app.ml.features import build_features, feature_json, write_snapshots
from tests.helpers import purge_tenants, seed_tiny

D = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)  # a Wednesday, so no weekly anchor shares the key
D_NEXT = D + timedelta(days=1)
LATE_OCCURRED = D - timedelta(days=1)
LATE_RECEIVED = D + timedelta(hours=12)  # after D, before D+1


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny"))
    yield ids
    purge_tenants(database_url, ids)


def stored(conn, as_of) -> dict:
    """The tenant's stored nightly snapshots at ``as_of``: account_id -> feature dict."""
    rows = conn.execute(
        sa.text("SELECT account_id, features FROM snapshots WHERE as_of = :t AND kind = 'nightly'"),
        {"t": as_of},
    ).all()
    return {r.account_id: r.features for r in rows}


def recomputed(conn, tid, as_of) -> dict:
    """What the featurizer says now for ``as_of``, in the stored JSON form."""
    frame = build_features(conn, tid, as_of)
    return {
        account_id: json.loads(feature_json(values))
        for account_id, values in zip(frame.index, frame.to_numpy().tolist(), strict=True)
    }


def test_late_event_leaves_snapshot_d_unchanged_and_changes_the_next(seeded):
    tid = seeded[0]
    with tenant_tx(tid) as conn:
        # Night D, as the job does it: resolve what has arrived, then snapshot.
        resolve(conn, tid, D)
        assert write_snapshots(conn, tid, D, "nightly") > 0
        snapshot_d = stored(conn, D)
        assert snapshot_d

        # Night D+1 before the late event exists: the baseline for the next snapshot.
        resolve(conn, tid, D_NEXT)
        before_next = recomputed(conn, tid, D_NEXT)

        # The account that gets the late ticket: active at D, with a Zendesk identity.
        account_id, org_id = conn.execute(
            sa.text(
                "SELECT account_id, external_id FROM crosswalk WHERE source = 'zendesk'"
                " AND account_id = ANY(:ids) ORDER BY account_id LIMIT 1"
            ),
            {"ids": list(snapshot_d)},
        ).one()

        # The late ticket: occurred a day before D, received twelve hours after it.
        payload = {
            "organization_id": org_id,
            "ticket_id": 999999,
            "requester_email": "qa@late.example",
            "status": "open",
            "priority": "normal",
            "csat": None,
        }
        conn.execute(
            sa.text(
                "INSERT INTO raw_events (tenant_id, source, source_event_id, source_schema_version,"
                " event_type, occurred_at, received_at, payload)"
                " VALUES (:tid, 'zendesk', 'qa-late-1', 2, 'ticket_created', :occ, :rec,"
                " CAST(:payload AS jsonb))"
            ),
            {
                "tid": tid,
                "occ": LATE_OCCURRED,
                "rec": LATE_RECEIVED,
                "payload": json.dumps(payload),
            },
        )
        # Invisible to a resolve at D: it has not arrived yet.
        assert resolve(conn, tid, D).normalised == 0
        # Visible to D+1: the one new event is matched by crosswalk.
        assert resolve(conn, tid, D_NEXT).by_method == {"crosswalk": 1}

        # Snapshot D: the stored rows are exactly what they were ...
        assert stored(conn, D) == snapshot_d
        assert write_snapshots(conn, tid, D, "nightly") == 0  # a rebuild writes nothing new
        assert stored(conn, D) == snapshot_d
        # ... and recomputing D, now that the event sits in normalised_events, gives them again.
        assert recomputed(conn, tid, D) == snapshot_d

        # Snapshot D+1: differs for the event's account, and only for that account.
        after_next = recomputed(conn, tid, D_NEXT)
        assert set(after_next) == set(before_next)
        assert {a for a in after_next if after_next[a] != before_next[a]} == {account_id}
        old, new = before_next[account_id], after_next[account_id]
        assert new["tickets_30d"] == old["tickets_30d"] + 1
        assert new["total_event_count"] == old["total_event_count"] + 1
        assert new["ticket_rate_30d_vs_prior_90d"] > old["ticket_rate_30d_vs_prior_90d"]

        # Stored, the next snapshot carries it, and its watermark covers the late arrival.
        write_snapshots(conn, tid, D_NEXT, "nightly")
        assert stored(conn, D_NEXT)[account_id]["tickets_30d"] == new["tickets_30d"]
        watermark = conn.execute(
            sa.text("SELECT max(ingest_watermark) FROM snapshots WHERE as_of = :t"), {"t": D_NEXT}
        ).scalar_one()
        assert watermark >= LATE_RECEIVED


def test_a_stored_snapshot_cannot_be_rewritten_by_the_runtime_role(seeded):
    """The other half of "never restated": no UPDATE or DELETE privilege on snapshots."""
    with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
        with tenant_tx(seeded[0]) as conn:
            conn.execute(sa.text("UPDATE snapshots SET features = '{}'::jsonb"))
    with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
        with tenant_tx(seeded[0]) as conn:
            conn.execute(sa.text("DELETE FROM snapshots"))
