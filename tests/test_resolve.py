"""Resolve tests: one row per match method, each quarantine reason, the as_of rule and reruns.

The tenant is built by hand so every event is there for a reason. The pure ``decide`` function is
tested without a database; the rest runs ``resolve`` inside ``tenant_tx``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.db import copy_insert, get_engine, make_engine, tenant_tx
from app.ingest.loader import RAW_COLUMNS
from app.ingest.resolve import decide, email_domain, ingest_watermark, resolve
from tests.helpers import purge_tenants

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000000e2")
A1, A2, A3 = (uuid.UUID(f"00000000-0000-4000-8000-0000000000a{n}") for n in (1, 2, 3))

AS_OF = datetime(2026, 10, 12, tzinfo=UTC)
EARLY = AS_OF - timedelta(days=1)

# A1 owns a1.example. A2 and A3 share shared.example, so that domain is ambiguous.
CROSSWALK = [
    ("salesforce", "SF1", A1),
    ("stripe", "cus_1", A1),
    ("product", "ws_2", A2),
    ("zendesk", "ZD2", A2),
]


# --- decide: pure, no database ------------------------------------------------------------


@pytest.fixture
def lookups():
    crosswalk = {(s, k): a for s, k, a in CROSSWALK}
    domains = {"a1.example": [A1], "shared.example": [A2, A3]}
    return crosswalk, domains


@pytest.mark.parametrize(
    ("source", "payload", "account", "method", "reason"),
    [
        ("salesforce", {"account_id": "SF1"}, A1, "crosswalk", None),
        ("stripe", {"customer": "cus_1"}, A1, "crosswalk", None),
        # The crosswalk wins even when the email points at another account.
        (
            "zendesk",
            {"organization_id": "ZD2", "requester_email": "x@a1.example"},
            A2,
            "crosswalk",
            None,
        ),
        (
            "zendesk",
            {"organization_id": "nope", "requester_email": "x@a1.example"},
            A1,
            "email_domain",
            None,
        ),
        (
            "zendesk",
            {"organization_id": None, "requester_email": "x@A1.Example "},
            A1,
            "email_domain",
            None,
        ),
        ("gmail", {"counterpart_email": "Bob@A1.example"}, A1, "email_domain", None),
        ("stripe", {"customer": "cus_unknown"}, None, None, "unknown_native_key"),
        ("product", {"workspace_id": "ws_unknown"}, None, None, "unknown_native_key"),
        ("gmail", {"counterpart_email": "me@gmail.com"}, None, None, "unknown_domain"),
        ("gmail", {"counterpart_email": "x@shared.example"}, None, None, "ambiguous_domain"),
        (
            "zendesk",
            {"organization_id": "nope", "requester_email": "x@shared.example"},
            None,
            None,
            "ambiguous_domain",
        ),
        (
            "zendesk",
            {"organization_id": "nope", "requester_email": "x@gmail.com"},
            None,
            None,
            "unknown_native_key",
        ),
        (
            "zendesk",
            {"organization_id": None, "requester_email": "x@gmail.com"},
            None,
            None,
            "unknown_domain",
        ),
        ("gmail", {"counterpart_email": None}, None, None, "no_identity"),
        ("gmail", {"counterpart_email": "not-an-address"}, None, None, "no_identity"),
        ("stripe", {}, None, None, "no_identity"),
        # Salesforce, Stripe and product have no email field, so an email never rescues them.
        (
            "stripe",
            {"customer": "cus_unknown", "requester_email": "x@a1.example"},
            None,
            None,
            "unknown_native_key",
        ),
    ],
)
def test_decide(lookups, source, payload, account, method, reason):
    d = decide(source, payload, *lookups)
    assert (d.account_id, d.match_method, d.reason) == (account, method, reason)


def test_native_keys_compare_as_strings(lookups):
    """A source that sends its key as a number still matches the text in the crosswalk."""
    crosswalk = {("zendesk", "360000"): A1}
    assert decide("zendesk", {"organization_id": 360000}, crosswalk, {}).account_id == A1


def test_email_domain():
    assert email_domain("Bob@Example.COM") == "example.com"
    assert email_domain("a@b@c.example") == "c.example"
    assert email_domain("nobody") is None
    assert email_domain("trailing@") is None
    assert email_domain(None) is None


# --- resolve: against Postgres ------------------------------------------------------------


def raw(n, source, event_type, payload, received_at=EARLY, occurred_at=None):
    return (
        TENANT,
        source,
        f"evt-{n}",
        1,
        event_type,
        occurred_at or received_at - timedelta(hours=1),
        received_at,
        json.dumps(payload),
    )


EVENTS = [
    raw(1, "salesforce", "opportunity_stage_changed", {"account_id": "SF1", "stage": "closed_won"}),
    raw(2, "stripe", "invoice.paid", {"customer": "cus_1", "amount_usd": 10.5, "attempt": 1}),
    raw(
        3,
        "zendesk",
        "ticket_created",
        {"organization_id": "ZD2", "requester_email": "q@a1.example", "status": "open"},
    ),
    raw(
        4,
        "zendesk",
        "ticket_created",
        {"organization_id": "gone", "requester_email": "q@a1.example", "status": "open"},
    ),
    raw(5, "gmail", "message", {"counterpart_email": "Q@A1.EXAMPLE", "direction": "inbound"}),
    raw(6, "stripe", "invoice.paid", {"customer": "cus_missing"}),
    raw(7, "gmail", "message", {"counterpart_email": "someone@gmail.com"}),
    raw(8, "gmail", "message", {"counterpart_email": "q@shared.example"}),
    raw(9, "gmail", "message", {"direction": "outbound"}),
]
EXPECTED_QUARANTINE = {
    "evt-6": "unknown_native_key",
    "evt-7": "unknown_domain",
    "evt-8": "ambiguous_domain",
    "evt-9": "no_identity",
}
EXPECTED_MATCH = {
    "evt-1": ("crosswalk", A1),
    "evt-2": ("crosswalk", A1),
    "evt-3": ("crosswalk", A2),
    "evt-4": ("email_domain", A1),
    "evt-5": ("email_domain", A1),
}


@pytest.fixture(scope="module")
def tenant(database_url):
    """One tenant with three accounts, a small crosswalk and the nine events above."""
    ids = [TENANT, OTHER]
    purge_tenants(database_url, ids)
    with get_engine("app").begin() as conn:
        for tid in ids:
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (tenant_id, slug, name, segment, top_k,"
                    " contraction_threshold) VALUES (:t, :s, :s, 'smb', 8, 0.3)"
                ),
                {"t": tid, "s": f"resolve-{str(tid)[-1]}"},
            )
    with tenant_tx(TENANT) as conn:
        accounts = [
            (TENANT, A1, "One", "a1.example", None, EARLY),
            (TENANT, A2, "Two", "shared.example", None, EARLY),
            (TENANT, A3, "Three", "SHARED.example", None, EARLY),
        ]
        copy_insert(
            conn,
            "accounts",
            ("tenant_id", "account_id", "name", "website_domain", "csm_id", "started_at"),
            accounts,
        )
        copy_insert(
            conn,
            "crosswalk",
            ("tenant_id", "source", "external_id", "account_id"),
            [(TENANT, s, k, a) for s, k, a in CROSSWALK],
        )
    yield TENANT
    purge_tenants(database_url, ids)


@pytest.fixture
def events(tenant, database_url):
    """Reset the event tables to hold exactly EVENTS as unprocessed raw rows."""
    admin = make_engine(database_url, poolclass=sa.pool.NullPool)  # the runtime roles cannot DELETE
    with admin.begin() as conn:
        for table in ("quarantine", "normalised_events", "raw_events"):
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"),
                {"ids": [TENANT, OTHER]},
            )
    admin.dispose()
    with tenant_tx(tenant) as conn:
        copy_insert(conn, "raw_events", RAW_COLUMNS, EVENTS)
    return tenant


def run(tenant, as_of=AS_OF):
    with tenant_tx(tenant) as conn:
        return resolve(conn, tenant, as_of)


def snapshot(conn):
    return (
        conn.execute(
            sa.text(
                "SELECT n.raw_event_id, n.account_id, n.match_method, n.attrs"
                " FROM normalised_events n ORDER BY 1"
            )
        ).all(),
        conn.execute(
            sa.text("SELECT raw_event_id, reason, detail FROM quarantine ORDER BY 1")
        ).all(),
    )


def test_each_match_method_and_each_quarantine_reason(events):
    counts = run(events)
    assert counts.normalised == 5 and counts.quarantined == 4
    assert counts.by_method == {"crosswalk": 3, "email_domain": 2}
    assert counts.by_reason == {
        "unknown_native_key": 1,
        "unknown_domain": 1,
        "ambiguous_domain": 1,
        "no_identity": 1,
    }
    with tenant_tx(events) as conn:
        matched = {
            r.source_event_id: (r.match_method, r.account_id)
            for r in conn.execute(
                sa.text(
                    "SELECT r.source_event_id, n.match_method, n.account_id"
                    " FROM normalised_events n JOIN raw_events r USING (raw_event_id)"
                )
            )
        }
        quarantined = {
            r.source_event_id: r.reason
            for r in conn.execute(
                sa.text(
                    "SELECT r.source_event_id, q.reason"
                    " FROM quarantine q JOIN raw_events r USING (raw_event_id)"
                )
            )
        }
    assert matched == EXPECTED_MATCH
    assert quarantined == EXPECTED_QUARANTINE


def test_normalised_rows_link_to_raw_and_drop_only_identity_fields(events):
    run(events)
    with tenant_tx(events) as conn:
        row = conn.execute(
            sa.text(
                "SELECT n.*, r.received_at AS raw_received FROM normalised_events n"
                " JOIN raw_events r USING (raw_event_id) WHERE r.source_event_id = 'evt-3'"
            )
        ).one()
        detail = conn.execute(
            sa.text(
                "SELECT detail FROM quarantine q JOIN raw_events r USING (raw_event_id)"
                " WHERE r.source_event_id = 'evt-8'"
            )
        ).scalar_one()
    assert (row.source, row.event_type) == ("zendesk", "ticket_created")
    assert row.received_at == row.raw_received == EARLY
    assert row.resolved_as_of == AS_OF
    assert row.attrs == {"status": "open"}  # organization_id and requester_email are identity
    assert detail == {"domain": "shared.example", "accounts": 2}


def test_an_event_received_after_as_of_is_not_processed_until_a_later_as_of(events):
    late = raw(
        10,
        "stripe",
        "invoice.paid",
        {"customer": "cus_1"},
        received_at=AS_OF + timedelta(minutes=1),
    )
    on_time = raw(11, "stripe", "invoice.paid", {"customer": "cus_1"}, received_at=AS_OF)
    with tenant_tx(events) as conn:
        copy_insert(conn, "raw_events", RAW_COLUMNS, [late, on_time])

    first = run(events)
    assert first.normalised == 6  # the five matched plus evt-11, received exactly at as_of
    with tenant_tx(events) as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM raw_events")).scalar_one() == 11
        done = (
            conn.execute(
                sa.text(
                    "SELECT source_event_id FROM raw_events r WHERE EXISTS"
                    " (SELECT 1 FROM normalised_events n WHERE n.raw_event_id = r.raw_event_id)"
                )
            )
            .scalars()
            .all()
        )
    assert "evt-10" not in done and "evt-11" in done

    second = run(events, AS_OF + timedelta(minutes=1))
    assert (second.normalised, second.quarantined) == (1, 0)
    with tenant_tx(events) as conn:
        late_row = conn.execute(
            sa.text(
                "SELECT n.resolved_as_of FROM normalised_events n JOIN raw_events r"
                " USING (raw_event_id) WHERE r.source_event_id = 'evt-10'"
            )
        ).scalar_one()
    assert late_row == AS_OF + timedelta(minutes=1)


def test_rerunning_with_the_same_as_of_changes_nothing(events):
    first = run(events)
    with tenant_tx(events) as conn:
        before = snapshot(conn)
    again = run(events)
    assert (again.normalised, again.quarantined, again.by_method) == (0, 0, {})
    with tenant_tx(events) as conn:
        assert snapshot(conn) == before
    assert first.normalised + first.quarantined == len(EVENTS)


def test_chunks_cover_every_event_exactly_once(events):
    """A chunk size of 2 forces several round trips; the outcome equals a single pass."""
    with tenant_tx(events) as conn:
        counts = resolve(conn, events, AS_OF, chunk_size=2)
        rows = snapshot(conn)
    assert (counts.normalised, counts.quarantined) == (5, 4)
    assert len(rows[0]) == 5 and len(rows[1]) == 4


def test_ingest_watermark_is_the_newest_normalised_arrival_by_as_of(events):
    with tenant_tx(events) as conn:
        assert ingest_watermark(conn, AS_OF) is None  # nothing resolved yet
    t = AS_OF - timedelta(hours=6)
    newer_unmatched = raw(
        12, "stripe", "invoice.paid", {"customer": "cus_missing"}, received_at=AS_OF
    )
    newer_matched = raw(13, "stripe", "invoice.paid", {"customer": "cus_1"}, received_at=t)
    after = raw(
        14, "stripe", "invoice.paid", {"customer": "cus_1"}, received_at=AS_OF + timedelta(days=1)
    )
    with tenant_tx(events) as conn:
        copy_insert(conn, "raw_events", RAW_COLUMNS, [newer_unmatched, newer_matched, after])
    run(events, AS_OF + timedelta(days=1))
    with tenant_tx(events) as conn:
        assert ingest_watermark(conn, AS_OF) == t  # quarantined and later rows do not count
        assert ingest_watermark(conn, AS_OF + timedelta(days=1)) == AS_OF + timedelta(days=1)
        assert ingest_watermark(conn, t - timedelta(days=30)) is None


def test_resolve_for_one_tenant_ignores_the_others_events(events):
    with tenant_tx(OTHER) as conn:
        copy_insert(
            conn,
            "raw_events",
            RAW_COLUMNS,
            [
                (
                    OTHER,
                    "stripe",
                    "x-1",
                    1,
                    "invoice.paid",
                    EARLY,
                    EARLY,
                    json.dumps({"customer": "cus_1"}),
                )
            ],
        )
    run(events)
    with tenant_tx(OTHER) as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM normalised_events")).scalar_one() == 0
        assert conn.execute(sa.text("SELECT count(*) FROM quarantine")).scalar_one() == 0
        counts = resolve(conn, OTHER, AS_OF)
    # OTHER has no crosswalk, so its own event is quarantined, not matched to TENANT's account.
    assert (counts.normalised, counts.quarantined) == (0, 1)
