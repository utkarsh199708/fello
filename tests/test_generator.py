"""Generator tests: determinism, id formats, safe domains, truth isolation and event counts."""

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from app.config import load_config, parse_utc
from app.generator.generate import generate
from app.generator.world import salesforce_id
from app.vocab import EVENT_TYPES, NATIVE_KEY, SOURCES

ROOT = Path(__file__).resolve().parent.parent
TINY = ROOT / "tests" / "fixtures" / "tiny.yaml"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_events(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    out = tmp_path_factory.mktemp("tiny")
    cfg = load_config(TINY)
    manifest = generate(cfg, out)
    return cfg, out, manifest


def test_two_runs_give_identical_bytes_and_only_lf(tiny, tmp_path):
    cfg, out, _ = tiny
    generate(cfg, tmp_path)
    first, second = tree(out), tree(tmp_path)
    assert first.keys() == second.keys()
    for name in first:
        assert first[name] == second[name], name
        assert b"\r" not in first[name], f"{name} has a carriage return"
    assert first["tenants.csv"].endswith(b"\n")


def test_manifest_hashes_match_files(tiny):
    import hashlib

    _, out, manifest = tiny
    on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in on_disk["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert on_disk["counts"].keys() == {"t1", "t2"}
    assert on_disk["config_sha256"] == load_config(TINY).sha256


def test_a_different_seed_changes_the_data(tiny, tmp_path):
    cfg, out, _ = tiny
    from dataclasses import replace

    generate(replace(cfg, seed=cfg.seed + 1), tmp_path)
    assert (tmp_path / "events" / "t1.jsonl").read_bytes() != (
        out / "events" / "t1.jsonl"
    ).read_bytes()


def test_native_id_formats_and_uniqueness(tiny):
    _, out, _ = tiny
    rows = read_csv(out / "crosswalk.csv")
    by_source = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row["external_id"])
    assert all(len(i) == 18 and i.startswith("001") for i in by_source["salesforce"])
    assert all(i.startswith("cus_") and len(i) == 18 for i in by_source["stripe"])
    assert all(i.isdigit() for i in by_source["zendesk"])
    assert all(i.startswith("ws_") for i in by_source["product"])
    for ids in by_source.values():
        assert len(ids) == len(set(ids))
    # The checksum is Salesforce's real one: recomputing it from the first 15 characters agrees.
    for sf in by_source["salesforce"]:
        assert salesforce_id(sf[:3], sf[3:15]) == sf


def test_salesforce_checksum_follows_the_published_rule():
    # Blocks "001D0", "00000", "IqhSL". Bit i is set for an upper-case letter at position i:
    # D at 3 gives 8 -> "I"; no capitals gives 0 -> "A"; I, S, L at 0, 3, 4 give 25 -> "Z".
    assert salesforce_id("001", "D000000IqhSL") == "001D000000IqhSLIAZ"


def test_crosswalk_covers_every_native_key_in_events(tiny):
    _, out, _ = tiny
    tenants = {r["slug"]: r["tenant_id"] for r in read_csv(out / "tenants.csv")}
    known = {
        (r["tenant_id"], r["source"], r["external_id"]) for r in read_csv(out / "crosswalk.csv")
    }
    for slug, tenant_id in tenants.items():
        for event in read_events(out / "events" / f"{slug}.jsonl"):
            source = event["source"]
            if source in NATIVE_KEY:
                key = event["payload"][NATIVE_KEY[source]]
                assert (tenant_id, source, key) in known, (slug, source, key)


def test_payloads_carry_the_fields_the_vocabulary_names(tiny):
    _, out, _ = tiny
    for slug in ("t1", "t2"):
        for event in read_events(out / "events" / f"{slug}.jsonl"):
            wanted = EVENT_TYPES[event["source"]][event["event_type"]]
            assert set(wanted) == set(event["payload"]), event


def test_only_reserved_domains_appear(tiny):
    _, out, _ = tiny
    address = re.compile(rb"[A-Za-z0-9._+-]+@([A-Za-z0-9.-]+)")
    seen = set()
    for name, data in tree(out).items():
        for match in address.finditer(data):
            domain = match.group(1).decode().rstrip(".")
            seen.add(domain)
            assert domain.endswith((".example", ".test")), (name, domain)
    assert seen  # the check ran against real addresses


def test_health_appears_only_under_truth(tiny):
    _, out, _ = tiny
    for name, data in tree(out).items():
        if name.startswith("truth/") or name == "manifest.json":
            continue
        assert b"health" not in data.lower(), name
        assert b"unobserved" not in data.lower(), name
    truth = {p.name for p in (out / "truth").iterdir()}
    assert "health_monthly.csv" in truth
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert all(n.startswith("truth/") for n in manifest["files"] if "health" in n)


def test_timestamps_lag_and_the_live_tail(tiny):
    cfg, out, _ = tiny
    horizon_end = cfg.horizon_end
    late = 0
    for slug in ("t1", "t2"):
        events = read_events(out / "events" / f"{slug}.jsonl")
        keys = [(e["received_at"], e["source"], e["source_event_id"]) for e in events]
        assert keys == sorted(keys)
        for e in events:
            occurred, arrived = parse_utc(e["occurred_at"]), parse_utc(e["received_at"])
            assert arrived >= occurred
            assert occurred <= horizon_end
            late += arrived > cfg.train_as_of
    assert (
        late > 0
    )  # events whose received_at is after train_as_of: the tail the nightly job ingests


def test_sources_respect_connected_at(tiny):
    cfg, out, _ = tiny
    for tcfg in cfg.tenants:
        events = read_events(out / "events" / f"{tcfg.slug}.jsonl")
        assert {e["source"] for e in events} == set(tcfg.connected_at)
        for e in events:
            assert parse_utc(e["occurred_at"]) >= tcfg.connected_at[e["source"]]
    assert "gmail" not in {e["source"] for e in read_events(out / "events" / "t2.jsonl")}


def test_contracts_are_annual_and_agree_with_renewal_events(tiny):
    _, out, _ = tiny
    contracts = defaultdict(list)
    for r in read_csv(out / "contracts.csv"):
        contracts[r["account_id"]].append(r)
    for rows in contracts.values():
        rows.sort(key=lambda r: int(r["cycle_number"]))
        for i, row in enumerate(rows):
            start = datetime.fromisoformat(row["start_date"])
            end = datetime.fromisoformat(row["end_date"])
            assert end.year - start.year == 1 and (end - start).days in (365, 366)
            assert int(row["cycle_number"]) == i + 1
            if i:
                assert row["start_date"] == rows[i - 1]["end_date"]

    accounts = {r["account_id"]: r for r in read_csv(out / "accounts.csv")}
    by_sf = {r["external_id"]: r["account_id"] for r in read_csv(out / "crosswalk.csv")}
    outcomes = {
        (r["account_id"], r["renewal_date"]): r
        for r in read_csv(out / "truth/renewal_outcomes.csv")
    }
    seen = 0
    for slug in ("t1", "t2"):
        for e in read_events(out / "events" / f"{slug}.jsonl"):
            if e["event_type"] != "opportunity_stage_changed":
                continue
            p = e["payload"]
            account_id = by_sf[p["account_id"]]
            truth = outcomes[(account_id, p["renewal_date"])]
            assert (p["stage"] == "closed_lost") == (truth["outcome"] == "churned")
            assert p["renewed_arr"] == float(truth["renewed_arr"])
            nxt = [c for c in contracts[account_id] if c["start_date"] == p["renewal_date"]]
            assert len(nxt) == (0 if p["stage"] == "closed_lost" else 1)
            if nxt:
                assert float(nxt[0]["arr"]) == p["renewed_arr"]
            seen += 1
    assert seen > 10
    assert len(accounts) == 100


def test_stripe_and_salesforce_agree_on_every_renewal(tiny):
    _, out, _ = tiny
    events = read_events(out / "events" / "t1.jsonl")
    sf_lost = {
        e["payload"]["account_id"]
        for e in events
        if e["event_type"] == "opportunity_stage_changed" and e["payload"]["stage"] == "closed_lost"
    }
    accounts_by_sf = {
        r["external_id"]: r["account_id"]
        for r in read_csv(out / "crosswalk.csv")
        if r["source"] == "salesforce"
    }
    accounts_by_stripe = {
        r["external_id"]: r["account_id"]
        for r in read_csv(out / "crosswalk.csv")
        if r["source"] == "stripe"
    }
    deleted = {
        accounts_by_stripe[e["payload"]["customer"]]
        for e in events
        if e["event_type"] == "subscription.deleted"
    }
    assert {accounts_by_sf[i] for i in sf_lost} == deleted


def test_csms_hold_about_the_configured_number_of_accounts(tiny):
    _, out, _ = tiny
    per_csm = defaultdict(int)
    for r in read_csv(out / "accounts.csv"):
        per_csm[r["csm_id"]] += 1
    assert set(per_csm.values()) <= {20, 21, 22}  # 60 accounts over 3 CSMs, 40 over 2


def test_config_dates_and_ids():
    for name in ("small", "full"):
        cfg = load_config(ROOT / "config" / f"{name}.yaml")
        assert cfg.train_as_of.weekday() == 0 and cfg.eval_as_of.weekday() == 0
        assert cfg.horizon_start < cfg.train_as_of <= cfg.horizon_end
        assert len({t.tenant_id for t in cfg.tenants}) == len(cfg.tenants)
    assert parse_utc("2026-10-12").isoformat() == "2026-10-12T00:00:00+00:00"
    assert parse_utc("2026-10-12T02:00:00+02:00").hour == 0


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    out = tmp_path_factory.mktemp("small")
    return (
        load_config(ROOT / "config" / "small.yaml"),
        out,
        generate(load_config(ROOT / "config" / "small.yaml"), out),
    )


def test_small_config_lands_near_its_target(small):
    _, out, manifest = small
    assert len(read_csv(out / "accounts.csv")) == 500
    total = sum(sum(per.values()) for per in manifest["counts"].values())
    assert 18_000 <= total <= 22_000


def test_full_config_lands_near_five_thousand_accounts_and_two_hundred_thousand_events(tmp_path):
    cfg = load_config(ROOT / "config" / "full.yaml")
    manifest = generate(cfg, tmp_path)
    assert len(read_csv(tmp_path / "accounts.csv")) == 5000
    assert len(read_csv(tmp_path / "csms.csv")) == 43
    total = sum(sum(per.values()) for per in manifest["counts"].values())
    assert 180_000 <= total <= 220_000
    # source mix lands within 10 percentage points of the plan (product 38, zendesk 22, ...)
    by_source = {s: sum(per.get(s, 0) for per in manifest["counts"].values()) for s in SOURCES}
    assert by_source["product"] / total > 0.3 and by_source["zendesk"] / total > 0.15
    # the structural gaps from PLAN.md section 5
    assert "gmail" not in manifest["counts"]["t2"]
    assert "stripe" not in manifest["counts"]["t3"]
    assert "zendesk" not in manifest["counts"]["t5"]
