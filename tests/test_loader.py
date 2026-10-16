"""Loader tests: reference tables, raw events, idempotency, atomicity and the CLI.

The data is the tiny config under another name, so its tenants have ids of their own and this
module neither sees nor disturbs other modules' tenants. The ``world`` fixture removes them
before each test and at teardown, so every test starts from an empty database.
"""

from __future__ import annotations

import csv
import json
import shutil

import pytest
import sqlalchemy as sa

from app.config import load_config
from app.db import get_engine, tenant_tx
from app.generator.generate import generate
from app.ingest import cli
from app.ingest.loader import load_all, load_events, load_reference
from app.ingest.pii import PII_EMAIL_FIELDS, scrub, tenant_key
from tests.helpers import TINY, load_tiny, purge_tenants


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("loader")
    cfg_path = root / "loader.yaml"
    cfg_path.write_text(TINY.read_text(encoding="utf-8").replace("name: tiny", "name: tiny-loader"))
    cfg = load_config(cfg_path)
    generate(cfg, root / "data")
    return cfg, root / "data"


@pytest.fixture
def world(generated, database_url):
    """The generated config and data directory, with this module's tenants absent from the db."""
    cfg, out = generated
    ids = [t.tenant_id for t in cfg.tenants]
    purge_tenants(database_url, ids)
    yield cfg, out
    purge_tenants(database_url, ids)


def csv_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def jsonl_events(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def count(conn, table):
    return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_load_all_inserts_every_row_and_a_second_load_inserts_none(world):
    cfg, out = world
    first = load_all(out)
    assert first["reference"] == {
        "tenants": 2,
        "csms": len(csv_rows(out / "csms.csv")),
        "accounts": 100,
        "contracts": len(csv_rows(out / "contracts.csv")),
        "crosswalk": len(csv_rows(out / "crosswalk.csv")),
    }
    for t in cfg.tenants:
        assert first["events"][t.slug] == len(jsonl_events(out / "events" / f"{t.slug}.jsonl"))

    again = load_all(out)
    assert set(again["reference"].values()) == {0}
    assert set(again["events"].values()) == {0}
    for t in cfg.tenants:
        with tenant_tx(t.tenant_id) as conn:
            assert count(conn, "raw_events") == first["events"][t.slug]


def test_raw_events_keep_every_field_as_received_except_addresses(world):
    cfg, out = world
    load_all(out)
    t = cfg.tenants[0]
    expected = jsonl_events(out / "events" / f"{t.slug}.jsonl")
    key = tenant_key(t.tenant_id)
    with tenant_tx(t.tenant_id) as conn:
        rows = conn.execute(
            sa.text(
                "SELECT tenant_id, source, source_event_id, source_schema_version, event_type,"
                " occurred_at, received_at, payload FROM raw_events ORDER BY raw_event_id"
            )
        ).all()
    assert len(rows) == len(expected)
    for row, event in zip(rows, expected, strict=True):
        assert row.tenant_id == t.tenant_id
        assert (row.source, row.source_event_id) == (event["source"], event["source_event_id"])
        assert row.source_schema_version == event["source_schema_version"]
        assert row.event_type == event["event_type"]
        assert row.occurred_at.isoformat() == event["occurred_at"].replace("Z", "+00:00")
        assert row.received_at.isoformat() == event["received_at"].replace("Z", "+00:00")
        # Every field as received, except that addresses are pseudonymised (task 3.4).
        assert row.payload == scrub(event["payload"], key)
        for field in PII_EMAIL_FIELDS:
            if isinstance(event["payload"].get(field), str):
                local, _, domain = event["payload"][field].rpartition("@")
                assert row.payload[field].endswith("@" + domain)
                assert local not in row.payload[field]


def test_a_tenant_sees_only_its_own_rows(world):
    cfg, out = world
    load_all(out)
    a, b = (t.tenant_id for t in cfg.tenants)
    with tenant_tx(a) as conn:
        assert count(conn, "accounts") == 60
        assert conn.execute(
            sa.text("SELECT DISTINCT tenant_id FROM raw_events")
        ).scalars().all() == [a]
    with tenant_tx(b) as conn:
        assert count(conn, "accounts") == 40


def test_reference_rows_match_the_csv_files(world):
    cfg, out = world
    load_reference(out)
    t = cfg.tenants[0]
    contracts = [r for r in csv_rows(out / "contracts.csv") if r["tenant_id"] == str(t.tenant_id)]
    with tenant_tx(t.tenant_id) as conn:
        assert count(conn, "contracts") == len(contracts)
        first = conn.execute(
            sa.text("SELECT arr, start_date, end_date FROM contracts WHERE contract_id = :id"),
            {"id": contracts[0]["contract_id"]},
        ).one()
    assert str(first.arr) == contracts[0]["arr"]
    assert first.start_date.isoformat() == contracts[0]["start_date"]
    assert first.end_date.isoformat() == contracts[0]["end_date"]
    with get_engine("app").connect() as conn:
        connected = conn.execute(
            sa.text("SELECT connected_at FROM tenants WHERE tenant_id = :t"), {"t": t.tenant_id}
        ).scalar_one()
    assert set(connected) == set(t.connected_at)


def test_batches_split_the_file_and_lose_nothing(world):
    """With 100 rows per COPY, a 1,600-row file takes 17 batches and still loads whole."""
    cfg, out = world
    t = cfg.tenants[0]
    load_reference(out)
    loaded = load_events(out, t.tenant_id, batch_size=100)
    assert loaded == len(jsonl_events(out / "events" / f"{t.slug}.jsonl")) > 1000
    with tenant_tx(t.tenant_id) as conn:
        assert count(conn, "raw_events") == loaded
    assert load_events(out, t.tenant_id, batch_size=100) == 0


def test_empty_csv_fields_become_null(world, tmp_path):
    cfg, out = world
    data = tmp_path / "data"
    shutil.copytree(out, data)
    rows = csv_rows(data / "accounts.csv")
    t = cfg.tenants[1]
    unassigned = next(r for r in rows if r["tenant_id"] == str(t.tenant_id))
    unassigned["csm_id"] = ""
    with open(data / "accounts.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    load_reference(data)
    with tenant_tx(t.tenant_id) as conn:
        csm = conn.execute(
            sa.text("SELECT csm_id FROM accounts WHERE account_id = :a"),
            {"a": unassigned["account_id"]},
        ).scalar_one()
        assert csm is None
        assert count(conn, "accounts") == 40


def test_a_bad_line_loads_nothing(world, tmp_path):
    cfg, out = world
    t = cfg.tenants[0]
    data = tmp_path / "data"
    shutil.copytree(out, data)
    path = data / "events" / f"{t.slug}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = '{"source": "stripe"}'  # missing every other field
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_reference(data)
    with pytest.raises(ValueError, match=f"{t.slug}.jsonl line {len(lines)}"):
        load_events(data, t.tenant_id, batch_size=100)  # earlier batches were already copied
    with tenant_tx(t.tenant_id) as conn:
        assert count(conn, "raw_events") == 0


def test_cli_load_with_tenant_filter_then_ingest(world, capsys):
    cfg, out = world
    t1, t2 = cfg.tenants
    assert cli.main(["load", "--data", str(out), "--tenant", t2.slug]) == 0
    printed = capsys.readouterr().out
    assert f"raw_events {t2.slug}" in printed and f"raw_events {t1.slug}" not in printed
    with tenant_tx(t2.tenant_id) as conn:
        assert count(conn, "raw_events") > 0

    assert cli.main(["ingest", "--as-of", "2026-10-12", "--tenant", str(t2.tenant_id)]) == 0
    assert "normalised=" in capsys.readouterr().out
    with tenant_tx(t2.tenant_id) as conn:
        assert count(conn, "normalised_events") > 0
        assert count(conn, "quarantine") == 0

    with pytest.raises(SystemExit):  # ingest without --as-of is a usage error
        cli.main(["ingest"])
    with pytest.raises(ValueError, match="not in tenants.csv"):
        load_all(out, "no-such-tenant")


def test_load_tiny_helper_is_idempotent(database_url, tmp_path):
    """Other test modules seed through this helper, so calling it twice must change nothing."""
    ids = load_tiny(tmp_path / "a")
    try:
        with tenant_tx(ids[0]) as conn:
            before = (count(conn, "raw_events"), count(conn, "normalised_events"))
        assert load_tiny(tmp_path / "b") == ids
        with tenant_tx(ids[0]) as conn:
            assert (count(conn, "raw_events"), count(conn, "normalised_events")) == before
        assert before[1] > 0
    finally:
        purge_tenants(database_url, ids)
