"""Step 2: the generator emits every kind of mess PLAN.md section 5 asks for, at its target rate.

The mess fixture is the tiny config with the real Step 2 knobs copied from config/small.yaml, so
these tests exercise the values that ship, on data small enough to generate in about a second.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml

from app.config import load_config, parse_utc
from app.db import tenant_tx
from app.generator.generate import generate
from app.generator.report import TARGET_MIX, build_report
from app.generator.world import holdout_window
from tests.helpers import ROOT, TINY, load_tiny, purge_tenants

FULL = ROOT / "config" / "full.yaml"
SMALL = ROOT / "config" / "small.yaml"


def write_mess_config(directory: Path) -> Path:
    """The tiny tenants, renamed and enlarged, with every Step 2 knob from config/small.yaml."""
    doc = yaml.safe_load(TINY.read_text(encoding="utf-8"))
    doc["name"] = "tiny-mess"
    doc["generator"] = yaml.safe_load(SMALL.read_text(encoding="utf-8"))["generator"]
    # The tiny history starts on the real Zendesk drift date, so move that cut half a year
    # later here to keep both versions in the data.
    doc["generator"]["drift"]["zendesk_priority_string_from"] = "2025-04-07T00:00:00Z"
    for tenant, slug, size in zip(doc["tenants"], ("m1", "m2"), (300, 200), strict=True):
        tenant["slug"], tenant["name"], tenant["accounts"] = slug, tenant["name"] + " Mess", size
    path = directory / "tiny_mess.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_events(path: Path) -> list[dict]:
    with path.open("rb") as fh:
        return [json.loads(line) for line in fh]


@pytest.fixture(scope="module")
def mess(tmp_path_factory):
    directory = tmp_path_factory.mktemp("mess")
    path = write_mess_config(directory)
    cfg = load_config(path)
    manifest = generate(cfg, directory / "data")
    return path, cfg, directory / "data", manifest


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    out = tmp_path_factory.mktemp("full")
    cfg = load_config(FULL)
    generate(cfg, out)
    return cfg, out


def test_every_kind_of_mess_is_present(mess):
    _, _, out, _ = mess
    report = {r["slug"]: r for r in build_report(out)}
    for r in report.values():
        # Duplicate kind 1: exact replays, on every connected source.
        assert all(rate > 0 for rate in r["replay_rate"].values()), r["replay_rate"]
        # Duplicate kind 2: unchanged ticket snapshots with a newer updated_at.
        assert r["resent_snapshots"] > 0
        # Schema drift: both versions of both drifting sources.
        assert set(r["versions"]["zendesk"]) == {1, 2}
        assert set(r["versions"]["salesforce"]) == {1, 2}
    # Identity breaks reach quarantine with every reason the resolve step knows.
    assert set(report["m1"]["quarantine"]) == {
        "ambiguous_domain",
        "unknown_domain",
        "unknown_native_key",
    }
    assert "unknown_native_key" in report["m2"]["quarantine"]  # m2 has no Gmail
    assert all(r["quarantine_rate"] < 0.05 for r in report.values())


def test_drift_changes_the_payload_exactly_as_the_vocabulary_says(mess):
    _, cfg, out, _ = mess
    drift = cfg.generator["drift"]
    priority_cut = drift["zendesk_priority_string_from"].replace("+00:00", "Z")
    arr_cut = drift["salesforce_arr_field_renamed_from"].replace("+00:00", "Z")
    gaps = Counter()
    for e in read_events(out / "events" / "m1.jsonl"):
        p, v = e["payload"], e["source_schema_version"]
        if e["source"] == "zendesk":
            assert v == (1 if e["occurred_at"] < priority_cut else 2)
            assert isinstance(p["priority"], int if v == 1 else str)
        if e["event_type"] == "account_upserted":
            assert v == (1 if e["occurred_at"] < arr_cut else 2)
            field = "arr__c" if v == 1 else "annual_contract_value__c"
            gaps["industry"] += p["industry"] is None
            gaps["arr"] += p[field] is None
            gaps["records"] += 1
    # Structured missingness in Salesforce: some records lack industry or ARR, most do not.
    assert 0 < gaps["industry"] < gaps["records"] * 0.25
    assert 0 < gaps["arr"] < gaps["records"] * 0.25


def test_identity_breaks_match_the_truth_that_caused_them(mess):
    _, cfg, out, _ = mess
    truth = {r["account_id"]: r for r in read_csv(out / "truth" / "account_mess.csv")}
    crosswalk = defaultdict(set)
    for r in read_csv(out / "crosswalk.csv"):
        crosswalk[r["account_id"]].add(r["source"])
    domains = Counter(r["website_domain"] for r in read_csv(out / "accounts.csv"))
    accounts = {r["account_id"]: r for r in read_csv(out / "accounts.csv")}
    assert any(t["missing_backref"] for t in truth.values())
    assert any(t["personal_contact"] == "1" for t in truth.values())
    for aid, t in truth.items():
        for source in filter(None, t["missing_backref"].split(";")):
            assert source not in crosswalk[aid]
        if t["thin_cause"] == "1":
            assert "product" not in crosswalk[aid]  # no workspace yet
        shared = domains[accounts[aid]["website_domain"]] > 1
        assert shared == (t["shared_domain"] == "1")
    freemail = cfg.generator["identity"]["freemail_domain"]
    mail = [e for e in read_events(out / "events" / "m1.jsonl") if e["source"] == "gmail"]
    assert any(e["payload"]["counterpart_email"].endswith("@" + freemail) for e in mail)


def test_planted_cases_fit_the_spec(mess):
    _, cfg, out, _ = mess
    lo, hi = holdout_window(cfg)
    planted = read_csv(out / "truth" / "planted_cases.csv")
    assert len(planted) == cfg.generator["planted"]["count"]
    outcomes = {
        (r["account_id"], r["renewal_date"]): r["outcome"]
        for r in read_csv(out / "truth" / "renewal_outcomes.csv")
    }
    accounts = {r["account_id"]: r for r in read_csv(out / "accounts.csv")}
    contracts = read_csv(out / "contracts.csv")
    mess_flags = {r["account_id"]: r for r in read_csv(out / "truth" / "account_mess.csv")}
    slugs = {str(t.tenant_id): t.slug for t in cfg.tenants}
    events = {slug: read_events(out / "events" / f"{slug}.jsonl") for slug in slugs.values()}
    crosswalk = {
        (r["source"], r["external_id"]): r["account_id"] for r in read_csv(out / "crosswalk.csv")
    }

    tickets_before = tickets_after = 0
    for case in planted:
        aid, renewal = case["account_id"], parse_utc(case["renewal_date"])
        signal = parse_utc(case["signal_start"])
        assert lo < renewal <= hi
        assert outcomes[(aid, case["renewal_date"])] == "churned"
        assert 10 <= int(case["lead_weeks"]) <= 12
        assert renewal - signal == timedelta(weeks=int(case["lead_weeks"]))
        flags = mess_flags[aid]
        assert (flags["missing_backref"], flags["personal_contact"], flags["shared_domain"]) == (
            "",
            "0",
            "0",
        )
        # ARR above the median of the CSM's book at the renewal date.
        csm = accounts[aid]["csm_id"]
        book = [
            float(c["arr"])
            for c in contracts
            if accounts[c["account_id"]]["csm_id"] == csm
            and parse_utc(c["start_date"]) < renewal <= parse_utc(c["end_date"])
        ]
        mine = next(
            float(c["arr"])
            for c in contracts
            if c["account_id"] == aid and parse_utc(c["end_date"]) == renewal
        )
        assert mine > statistics.median(book)
        # The signal is in the data: more tickets after signal_start than in as long before it.
        window = renewal - signal
        for e in events[slugs[accounts[aid]["tenant_id"]]]:
            if e["event_type"] != "ticket_created":
                continue
            if crosswalk.get(("zendesk", e["payload"]["organization_id"])) != aid:
                continue
            at = parse_utc(e["occurred_at"])
            tickets_after += signal <= at < renewal
            tickets_before += signal - window <= at < signal
    assert tickets_after > 2 * tickets_before


def test_mess_regenerates_byte_identical(mess, tmp_path):
    _, cfg, _, manifest = mess
    assert generate(cfg, tmp_path)["files"] == manifest["files"]


def test_the_clean_fixture_stays_clean(tmp_path):
    """Without Step 2 knobs there is no mess, which the Step 1 tests rely on."""
    generate(load_config(TINY), tmp_path)
    for r in build_report(tmp_path):
        assert all(rate == 0 for rate in r["replay_rate"].values())
        assert r["quarantine"] == {}
        assert set(r["versions"]["zendesk"]) == {2}
    assert read_csv(tmp_path / "truth" / "planted_cases.csv") == []


def test_full_config_meets_the_acceptance_targets(full):
    cfg, out = full
    report = build_report(out)
    totals = Counter()
    for r in report:
        totals.update(r["distinct"])
    grand = sum(totals.values())
    assert abs(grand - 200_000) / 200_000 < 0.10
    for source, target in TARGET_MIX.items():
        assert abs(totals[source] / grand - target) / target < 0.10, source
    accounts = sum(r["accounts"] for r in report)
    thin = sum(r["thin_share"] * r["accounts"] for r in report) / accounts
    assert 0.05 <= thin <= 0.11, thin
    assert sum(len(r["connected"]) < 5 for r in report) == 3  # three tenants miss a source
    by_slug = {r["slug"]: r for r in report}
    assert by_slug["t5"]["renewals"] == {}  # five months of history: no renewal yet
    assert all(r["quarantine_rate"] < 0.05 for r in report)

    # 20 to 25% of churn shows no visible signal: latent health still at or above the median
    # in the three months before the renewal (truth is read here, never in the pipeline).
    health = defaultdict(dict)
    for r in read_csv(out / "truth" / "health_monthly.csv"):
        health[r["account_id"]][r["month"]] = float(r["health"])

    def before(aid: str, day: str) -> float | None:
        d = datetime.fromisoformat(day)
        months = [
            (d.replace(day=1) - timedelta(days=28 * k + 1)).strftime("%Y-%m") for k in (0, 1, 2)
        ]
        values = [health[aid][m] for m in months if m in health[aid]]
        return float(np.mean(values)) if values else None

    seen = [
        (r["outcome"], before(r["account_id"], r["renewal_date"]))
        for r in read_csv(out / "truth" / "renewal_outcomes.csv")
    ]
    seen = [(o, h) for o, h in seen if h is not None]
    median = statistics.median(h for _, h in seen)
    churned = [h for o, h in seen if o == "churned"]
    silent = sum(h >= median for h in churned) / len(churned)
    assert 0.18 <= silent <= 0.27, silent
    assert len(read_csv(out / "truth" / "planted_cases.csv")) == 10


def test_pipeline_code_never_reads_truth():
    """Only evaluation may read data/truth/. Ingest, features, scoring, jobs and web never do."""
    for package in ("ingest", "ml", "jobs", "web"):
        for path in sorted((ROOT / "app" / package).rglob("*.py")):
            assert "truth" not in path.read_text(encoding="utf-8").lower(), path


@pytest.fixture(scope="module")
def mess_in_db(database_url, tmp_path_factory):
    directory = tmp_path_factory.mktemp("mess-db")
    path = write_mess_config(directory)
    ids = load_tiny(directory / "data", config=path)
    yield load_config(path), directory / "data", ids
    purge_tenants(database_url, ids)


def test_the_pipeline_quarantines_identity_breaks_and_loads_drift(mess_in_db):
    import sqlalchemy as sa

    _, _, ids = mess_in_db
    with tenant_tx(ids[0]) as conn:
        reasons = dict(
            conn.execute(sa.text("SELECT reason, count(*) FROM quarantine GROUP BY reason")).all()
        )
        methods = dict(
            conn.execute(
                sa.text("SELECT match_method, count(*) FROM normalised_events GROUP BY 1")
            ).all()
        )
        versions = conn.execute(
            sa.text("SELECT DISTINCT source, source_schema_version FROM raw_events")
        ).all()
    assert {"ambiguous_domain", "unknown_domain", "unknown_native_key"} <= set(reasons)
    assert methods.get("email_domain", 0) > 0  # Zendesk without a back-reference, by domain
    assert {("zendesk", 1), ("zendesk", 2), ("salesforce", 1), ("salesforce", 2)} <= set(versions)


def test_no_feature_is_a_copy_of_latent_health(mess_in_db):
    """A feature that tracks the generator's latent health exactly would mean truth leaked."""
    from app.ml.features import build_features

    cfg, out, ids = mess_in_db
    month = (cfg.train_as_of - timedelta(days=1)).strftime("%Y-%m")
    health = {
        r["account_id"]: float(r["health"])
        for r in read_csv(out / "truth" / "health_monthly.csv")
        if r["month"] == month
    }
    for tid in ids:
        with tenant_tx(tid) as conn:
            frame = build_features(conn, tid, cfg.train_as_of)
        latent = frame.index.astype(str).map(health).to_series(index=frame.index)
        for column in frame.columns:
            values = frame[column]
            both = values.notna() & latent.notna()
            if both.sum() < 10 or values[both].nunique() < 2:
                continue
            corr = np.corrcoef(values[both].astype(float), latent[both])[0, 1]
            assert abs(corr) < 0.99, (column, corr)
