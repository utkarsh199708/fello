"""Step 3: every kind of mess the generator makes has a handler, and each handler has a test.

Pure tests first (schema dispatch, pseudonymisation, the gate's verdicts), then database tests
on the tiny tenants: the three duplicate kinds, drift, order independence, the gate blocking a
night.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
import sqlalchemy as sa
import yaml

from app.config import load_config
from app.db import tenant_tx
from app.generator.generate import generate
from app.ingest.loader import load_all
from app.ingest.objects import ticket_states
from app.ingest.pii import hash_email, scrub, tenant_key
from app.ingest.resolve import renewal_date_for, resolve
from app.ingest.schema import SchemaError, canonical_payload
from app.jobs import gate
from app.web.main import missing_reason
from tests.helpers import TINY, load_tiny, purge_tenants, seed_tiny
from tests.test_generator_mess import write_mess_config

D = datetime(2026, 10, 12, tzinfo=UTC)  # the tiny config's train_as_of


# --- Schema dispatch (3.2) ------------------------------------------------------------------


def ticket(priority, **extra):
    return {"organization_id": "1", "ticket_id": 7, "status": "open", "priority": priority, **extra}


def test_zendesk_versions_normalise_to_one_shape():
    v1 = canonical_payload("zendesk", "ticket_updated", 1, ticket(3))
    v2 = canonical_payload("zendesk", "ticket_updated", 2, ticket("high"))
    assert v1 == v2 == ticket("high")


def test_salesforce_arr_field_rename_normalises_to_arr():
    base = {"account_id": "001x", "name": "A", "website_domain": "a.example"}
    year1 = canonical_payload("salesforce", "account_upserted", 1, {**base, "arr__c": 1200.0})
    year3 = canonical_payload(
        "salesforce", "account_upserted", 2, {**base, "annual_contract_value__c": 1200.0}
    )
    assert year1 == year3 == {**base, "arr": 1200.0}
    # A record with no ARR on file is NULL, not zero.
    blank = canonical_payload(
        "salesforce", "account_upserted", 2, {**base, "annual_contract_value__c": None}
    )
    assert blank["arr"] is None


@pytest.mark.parametrize(
    ("source", "event_type", "version", "payload", "reason"),
    [
        ("zendesk", "ticket_updated", 1, ticket("high"), "unparseable_payload"),  # v1 is a code
        ("zendesk", "ticket_updated", 1, ticket(True), "unparseable_payload"),  # bool is not 1
        ("zendesk", "ticket_updated", 2, ticket(3), "unparseable_payload"),  # v2 is a name
        ("zendesk", "ticket_updated", 2, ticket("whenever"), "unparseable_payload"),
        ("zendesk", "ticket_updated", 3, ticket("high"), "unknown_schema_version"),
        ("zendesk", "ticket_merged", 2, ticket("high"), "unknown_event_type"),
        ("salesforce", "account_upserted", 2, {"arr__c": 5.0}, "unparseable_payload"),
        ("salesforce", "account_upserted", 1, {"arr__c": "lots"}, "unparseable_payload"),
    ],
)
def test_unreadable_payloads_are_rejected_with_a_reason(
    source, event_type, version, payload, reason
):
    with pytest.raises(SchemaError) as caught:
        canonical_payload(source, event_type, version, payload)
    assert caught.value.reason == reason


# --- Pseudonymisation (3.4) -----------------------------------------------------------------


def test_email_local_parts_are_hashed_per_tenant_and_domains_kept():
    a = tenant_key("00000000-0000-4000-8000-0000000000a1", secret="s")
    b = tenant_key("00000000-0000-4000-8000-0000000000b1", secret="s")
    one = hash_email("Jordan.Rivera@T1-7.example", a)
    assert one == hash_email("jordan.rivera@t1-7.example", a)  # stable: a join key
    assert one.endswith("@t1-7.example") and "jordan" not in one
    assert one != hash_email("jordan.rivera@t1-7.example", b)  # not joinable across tenants
    assert hash_email(None, a) is None and hash_email("not an address", a) == "not an address"
    payload = {"requester_email": "x@y.example", "status": "open"}
    assert scrub(payload, a)["status"] == "open" and payload["requester_email"] == "x@y.example"


def test_loading_without_the_secret_is_refused(monkeypatch):
    monkeypatch.delenv("PII_HASH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="PII_HASH_SECRET"):
        tenant_key("00000000-0000-4000-8000-0000000000a1")


# --- NULL is not zero (3.3) -----------------------------------------------------------------


def test_a_null_feature_says_why_and_never_reads_as_zero():
    values = {
        "tickets_30d": None,
        "zendesk_connected": 0,
        "threads_90d": None,
        "gmail_connected": 1,
    }
    assert missing_reason("tickets_30d", values) == "Zendesk not connected"
    assert missing_reason("threads_90d", values) == "no data yet"
    assert missing_reason("arr", {"arr": 0}) is None  # a real zero is shown as a value


# --- The gate's verdicts (3.6) --------------------------------------------------------------

CONNECTED = {"zendesk": True, "product": True, "stripe": True, "gmail": False}
CLEAN_NULLS = {"tickets_30d": 0.0, "threads_90d": 1.0, "active_users_last_month": 0.1, "arr": 0.0}


def verdict(**overrides):
    args = dict(
        duplicates=0,
        resolved=1000,
        quarantined=10,
        null_rates=CLEAN_NULLS,
        connected=CONNECTED,
        mean_probability=0.12,
        previous_mean=0.11,
    )
    return gate.evaluate(**{**args, **overrides})


def test_a_clean_night_passes_the_gate():
    result = verdict()
    assert result.passed, result.failed
    assert result.checks["quarantine_rate"] == 0.01


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        ({"duplicates": 1}, "duplicates"),
        ({"quarantined": 100}, "quarantine_rate"),  # 10% of tonight's events
        ({"null_rates": {**CLEAN_NULLS, "active_users_last_month": 0.6}}, "null_rates"),
        ({"null_rates": {**CLEAN_NULLS, "threads_90d": 0.9}}, "null_rates"),  # Gmail unconnected
        ({"null_rates": {**CLEAN_NULLS, "arr": 0.01}}, "null_rates"),  # never NULL
        ({"mean_probability": 0.25}, "mean_shift"),
    ],
)
def test_each_check_trips_on_its_own(overrides, check):
    result = verdict(**overrides)
    assert [line.split(":")[0] for line in result.failed] == [check]


def test_a_new_model_skips_the_mean_shift_check():
    result = verdict(mean_probability=0.5, previous_mean=None)
    assert result.passed and result.checks["mean_shift"].startswith("skipped")


def test_stripe_renewals_match_the_contract_they_renew():
    ends = [date(2025, 6, 1), date(2026, 6, 1)]
    assert renewal_date_for(ends, datetime(2026, 6, 1, 13, tzinfo=UTC)) == date(2026, 6, 1)
    assert renewal_date_for(ends, datetime(2026, 6, 3, 23, tzinfo=UTC)) == date(2026, 6, 1)
    assert renewal_date_for(ends, datetime(2026, 6, 6, tzinfo=UTC)) is None


# --- Database: duplicates, drift and identity at the resolve step ---------------------------


@pytest.fixture(scope="module")
def tiny(database_url, tmp_path_factory):
    ids = load_tiny(tmp_path_factory.mktemp("tiny-hardening"))
    yield ids
    purge_tenants(database_url, ids)


def org_of(conn) -> tuple[str, str]:
    row = conn.execute(
        sa.text("SELECT account_id, external_id FROM crosswalk WHERE source = 'zendesk' LIMIT 1")
    ).one()
    return row.account_id, row.external_id


def insert_raw(conn, tid, source, event_id, version, event_type, occurred, received, payload):
    conn.execute(
        sa.text(
            "INSERT INTO raw_events (tenant_id, source, source_event_id, source_schema_version,"
            " event_type, occurred_at, received_at, payload)"
            " VALUES (:tid, :s, :e, :v, :t, :o, :r, CAST(:p AS jsonb))"
        ),
        {
            "tid": tid,
            "s": source,
            "e": event_id,
            "v": version,
            "t": event_type,
            "o": occurred,
            "r": received,
            "p": json.dumps(payload),
        },
    )


def outcome_of(conn, event_id: str) -> list[str]:
    """Where each delivery of a raw event ended up, in delivery order."""
    return (
        conn.execute(
            sa.text(
                """
            SELECT CASE WHEN n.raw_event_id IS NOT NULL THEN 'normalised'
                        WHEN q.raw_event_id IS NOT NULL THEN 'quarantine:' || q.reason
                        WHEN x.raw_event_id IS NOT NULL THEN 'rejects:' || x.reason
                        ELSE 'unprocessed' END
            FROM raw_events r
            LEFT JOIN normalised_events n USING (raw_event_id)
            LEFT JOIN quarantine q USING (raw_event_id)
            LEFT JOIN rejects x USING (raw_event_id)
            WHERE r.source_event_id = :e ORDER BY r.received_at
            """
            ),
            {"e": event_id},
        )
        .scalars()
        .all()
    )


def test_kind_1_an_exact_replay_is_rejected_and_the_first_delivery_kept(tiny):
    tid = tiny[0]
    with tenant_tx(tid) as conn:
        _, org = org_of(conn)
        payload = {**ticket("high", updated_at="2026-10-20T08:00:00Z"), "organization_id": org}
        # The replay is loaded first (lower raw id) but was received later: arrival time decides.
        insert_raw(conn, tid, "zendesk", "kind1", 2, "ticket_created",
                   D + timedelta(days=8), D + timedelta(days=9), payload)  # fmt: skip
        insert_raw(conn, tid, "zendesk", "kind1", 2, "ticket_created",
                   D + timedelta(days=8), D + timedelta(days=8, hours=1), payload)  # fmt: skip
        counts = resolve(conn, tid, D + timedelta(days=10))  # also takes the live tail's events
        assert counts.by_reject == {"duplicate_event_id": 1}
        assert outcome_of(conn, "kind1") == ["normalised", "rejects:duplicate_event_id"]
        kept = conn.execute(
            sa.text("SELECT received_at FROM normalised_events WHERE source_event_id = 'kind1'")
        ).scalar_one()
        assert kept == D + timedelta(days=8, hours=1)
        assert resolve(conn, tid, D + timedelta(days=10)).rejected == 0  # a rerun adds nothing
        # The unique index makes a second normalised row for the event impossible.
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text(
                    "INSERT INTO normalised_events (tenant_id, raw_event_id, source_event_id,"
                    " account_id, source, event_type, occurred_at, received_at, match_method,"
                    " resolved_as_of) SELECT tenant_id, raw_event_id, source_event_id, account_id,"
                    " source, event_type, occurred_at, received_at, match_method, resolved_as_of"
                    " FROM normalised_events WHERE source_event_id = 'kind1'"
                )
            )


def test_kind_2_resent_and_late_snapshots_resolve_to_the_newest_state(tiny):
    tid = tiny[0]
    with tenant_tx(tid) as conn:
        account, org = org_of(conn)

        def snap(event_id, status, updated, received):
            p = {**ticket("normal"), "organization_id": org, "ticket_id": 4242,
                 "status": status, "updated_at": updated.isoformat()}  # fmt: skip
            insert_raw(conn, tid, "zendesk", event_id, 2, "ticket_updated", updated, received, p)

        t0 = D + timedelta(days=11)
        snap("k2-open", "open", t0, t0 + timedelta(hours=1))
        snap("k2-solved", "solved", t0 + timedelta(days=1), t0 + timedelta(days=1, hours=1))
        snap("k2-resent", "solved", t0 + timedelta(days=2), t0 + timedelta(days=2, hours=1))
        # An older snapshot that arrives last must not reopen the ticket.
        snap("k2-late", "pending", t0 + timedelta(hours=5), t0 + timedelta(days=3))
        resolve(conn, tid, t0 + timedelta(days=4))
        states = [s for s in ticket_states(conn, t0 + timedelta(days=4)) if s["ticket_id"] == 4242]
        assert len(states) == 1 and states[0]["status"] == "solved"
        assert states[0]["account_id"] == account
        # Point in time: before the solved snapshot arrived, the ticket was open.
        earlier = [
            s for s in ticket_states(conn, t0 + timedelta(hours=2)) if s["ticket_id"] == 4242
        ]
        assert [s["status"] for s in earlier] == ["open"]


def test_drift_year_1_and_latest_rows_normalise_identically(tiny):
    tid = tiny[0]
    with tenant_tx(tid) as conn:
        _, org = org_of(conn)
        common = {**ticket(None), "organization_id": org, "ticket_id": 777, "updated_at": "x"}
        t = D + timedelta(days=15)
        insert_raw(
            conn, tid, "zendesk", "drift-v1", 1, "ticket_updated", t, t, {**common, "priority": 4}
        )
        insert_raw(
            conn,
            tid,
            "zendesk",
            "drift-v2",
            2,
            "ticket_updated",
            t,
            t,
            {**common, "priority": "urgent"},
        )
        insert_raw(
            conn,
            tid,
            "zendesk",
            "drift-bad",
            1,
            "ticket_updated",
            t,
            t,
            {**common, "priority": "urgent"},
        )
        resolve(conn, tid, t)
        assert outcome_of(conn, "drift-v1") == ["normalised"]
        assert outcome_of(conn, "drift-v2") == ["normalised"]
        assert outcome_of(conn, "drift-bad") == ["rejects:unparseable_payload"]
        attrs = (
            conn.execute(
                sa.text(
                    "SELECT attrs FROM normalised_events"
                    " WHERE source_event_id IN ('drift-v1', 'drift-v2')"
                )
            )
            .scalars()
            .all()
        )
        assert attrs[0] == attrs[1] and attrs[0]["priority"] == "urgent"
        detail = conn.execute(
            sa.text(
                "SELECT x.detail FROM rejects x JOIN raw_events r USING (raw_event_id)"
                " WHERE r.source_event_id = 'drift-bad'"
            )
        ).scalar_one()
        assert detail == {"priority": "urgent", "version": 1}


def test_kind_3_a_stripe_renewal_is_tied_to_its_contract(tiny):
    tid = tiny[0]
    with tenant_tx(tid) as conn:
        row = conn.execute(
            sa.text(
                "SELECT c.account_id, c.end_date, x.external_id FROM contracts c"
                " JOIN crosswalk x ON x.account_id = c.account_id AND x.source = 'stripe'"
                " ORDER BY c.end_date LIMIT 1"
            )
        ).one()
        at = datetime.combine(row.end_date, datetime.min.time(), UTC) + timedelta(hours=11)
        payload = {"customer": row.external_id, "subscription_id": "sub_x", "arr_usd": 0.0}
        insert_raw(conn, tid, "stripe", "k3-del", 1, "subscription.deleted", at, at, payload)
        resolve(conn, tid, at)
        attrs = conn.execute(
            sa.text("SELECT attrs FROM normalised_events WHERE source_event_id = 'k3-del'")
        ).scalar_one()
        assert attrs["renewal_date"] == row.end_date.isoformat()


# --- Database: order independence -------------------------------------------------------------


def two_copies(directory):
    """The same mess data under two config names (two tenant sets), one with shuffled lines."""
    paths = []
    for name, slugs in (("order-a", ("oa1", "oa2")), ("order-b", ("ob1", "ob2"))):
        doc = yaml.safe_load(write_mess_config(directory).read_text(encoding="utf-8"))
        doc["name"] = name
        for tenant, slug in zip(doc["tenants"], slugs, strict=True):
            tenant["slug"] = slug
        path = directory / f"{name}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


@pytest.fixture(scope="module")
def ordered_and_shuffled(database_url, tmp_path_factory):
    directory = tmp_path_factory.mktemp("order")
    loaded = []
    for path, shuffle in zip(two_copies(directory), (False, True), strict=True):
        cfg = load_config(path)
        out = directory / cfg.name
        generate(cfg, out)
        if shuffle:
            for events in sorted((out / "events").glob("*.jsonl")):
                lines = events.read_bytes().splitlines(keepends=True)
                random.Random(11).shuffle(lines)
                events.write_bytes(b"".join(lines))
        load_all(out)
        for t in cfg.tenants:
            with tenant_tx(t.tenant_id) as conn:
                resolve(conn, t.tenant_id, cfg.train_as_of)
        loaded.append((cfg, out))
    yield loaded
    purge_tenants(database_url, [t.tenant_id for cfg, _ in loaded for t in cfg.tenants])


def test_resolve_and_features_do_not_depend_on_arrival_order(ordered_and_shuffled):
    from app.ml.features import build_features

    (cfg_a, out_a), (cfg_b, out_b) = ordered_and_shuffled
    for ta, tb in zip(cfg_a.tenants, cfg_b.tenants, strict=True):
        frames, tallies = [], []
        for cfg, out, t in ((cfg_a, out_a, ta), (cfg_b, out_b, tb)):
            index = {
                r["account_id"]: i
                for i, r in enumerate(pd.read_csv(out / "accounts.csv").to_dict("records"))
                if r["tenant_id"] == str(t.tenant_id)
            }
            with tenant_tx(t.tenant_id) as conn:
                frame = build_features(conn, t.tenant_id, cfg.train_as_of)
                tallies.append(
                    {
                        table: dict(
                            conn.execute(
                                sa.text(f"SELECT reason, count(*) FROM {table} GROUP BY 1")  # noqa: S608
                            ).all()
                        )
                        for table in ("quarantine", "rejects")
                    }
                )
            frame.index = frame.index.astype(str).map(index)
            frames.append(frame.sort_index())
        pd.testing.assert_frame_equal(frames[0], frames[1])
        assert tallies[0] == tallies[1]
        assert tallies[0]["rejects"].get("duplicate_event_id", 0) > 0  # replays were there


# --- Database: the gate blocks a night ---------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny-gate"), TINY)
    yield ids
    purge_tenants(database_url, ids)


def test_a_tripped_gate_blocks_the_night_and_publishes_nothing(seeded, monkeypatch):
    from app.jobs.nightly import run_id_for, run_nightly

    night = datetime(2026, 10, 13, 2, tzinfo=UTC)
    monkeypatch.setattr(gate, "MAX_NULL_RATE", -1.0)  # every connected feature now "too NULL"
    runs = run_nightly(night, tenant=seeded[0])
    assert [r.status for r in runs] == ["blocked"]
    run_id = run_id_for(seeded[0], night.date())
    with tenant_tx(seeded[0]) as conn:
        row = conn.execute(
            sa.text("SELECT status, error, counts FROM job_runs WHERE run_id = :r"), {"r": run_id}
        ).one()
        published = conn.execute(
            sa.text("SELECT count(*) FROM scores WHERE run_id = :r"), {"r": run_id}
        ).scalar_one()
    assert row.status == "blocked" and row.error.startswith("data-quality gate: null_rates")
    assert "null_rates" in row.counts["gate"] and published == 0

    # Once the cause is fixed, the blocked night is claimed again and runs to completion.
    monkeypatch.undo()
    assert [r.status for r in run_nightly(night, tenant=seeded[0])] == ["succeeded"]
    with tenant_tx(seeded[0]) as conn:
        assert (
            conn.execute(
                sa.text("SELECT count(*) FROM scores WHERE run_id = :r"), {"r": run_id}
            ).scalar_one()
            > 0
        )
