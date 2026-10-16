"""`python -m app data-report [--data data]`: one screen per tenant on what the data looks like.

It reads only what the pipeline would see (the reference CSVs and the events files), never
data/truth/, so it describes the mess as a connector would deliver it (PLAN.md section 5).
Identity uses the resolve step's own `decide`, so the quarantine rate printed here is the one
ingest will produce. The output is deterministic and is committed to the README.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID

import numpy as np

from app.ingest.resolve import decide
from app.vocab import SOURCES

# The source mix PLAN.md section 5 asks for, as a share of distinct events.
TARGET_MIX = {"product": 0.38, "zendesk": 0.22, "stripe": 0.18, "gmail": 0.12, "salesforce": 0.08}
CONTRACTION = 0.70  # renewed below 70% of prior ARR counts as contracted (the label rule)
THIN = 5  # accounts with fewer distinct events than this are "thin"


def _hours(iso_a: str, iso_b: str) -> float:
    a = np.datetime64(iso_a.rstrip("Z"))
    b = np.datetime64(iso_b.rstrip("Z"))
    return float((b - a) / np.timedelta64(1, "s")) / 3600


def _ticket_state(p: dict) -> tuple:
    return (p["ticket_id"], p["status"], str(p["priority"]), p["csat"])


def tenant_report(data: Path, tenant: dict, accounts: list[dict], xwalk: list[dict]) -> dict:
    tid = tenant["tenant_id"]
    crosswalk = {(r["source"], r["external_id"]): UUID(r["account_id"]) for r in xwalk}
    domains: dict[str, list[UUID]] = defaultdict(list)
    for a in accounts:
        if a["website_domain"]:
            domains[a["website_domain"].lower()].append(UUID(a["account_id"]))

    rows = Counter()  # raw rows delivered, per source
    distinct = Counter()  # distinct source_event_id, per source
    versions: dict[str, Counter] = defaultdict(Counter)
    lags: dict[str, list[float]] = defaultdict(list)
    quarantine = Counter()
    per_account = Counter()
    resent = 0
    last_state: dict[str, tuple] = {}
    renewals: dict[str, Counter] = defaultdict(Counter)  # year -> outcome
    seen_ids: set[str] = set()

    with (data / "events" / f"{tenant['slug']}.jsonl").open("rb") as fh:
        for line in fh:
            e = json.loads(line)
            src = e["source"]
            rows[src] += 1
            if e["source_event_id"] in seen_ids:
                continue  # an exact replay: counted in rows only
            seen_ids.add(e["source_event_id"])
            distinct[src] += 1
            versions[src][e["source_schema_version"]] += 1
            lags[src].append(_hours(e["occurred_at"], e["received_at"]))
            p = e["payload"]
            d = decide(src, p, crosswalk, domains)
            if d.account_id is None:
                quarantine[d.reason] += 1
            else:
                per_account[d.account_id] += 1
            if src == "zendesk":
                key = f"{p['organization_id']}|{p['ticket_id']}"
                state = _ticket_state(p)
                if last_state.get(key) == state:
                    resent += 1  # same ticket state again, newer updated_at
                last_state[key] = state
            if (src, e["event_type"]) == ("salesforce", "opportunity_stage_changed"):
                if p["stage"] == "closed_lost":
                    outcome = "lost"
                elif p["renewed_arr"] < CONTRACTION * p["prior_arr"]:
                    outcome = "contracted"
                else:
                    outcome = "renewed"
                renewals[p["renewal_date"][:4]][outcome] += 1

    counts = np.array([per_account.get(UUID(a["account_id"]), 0) for a in accounts])
    total = sum(distinct.values())
    return {
        "tenant_id": tid,
        "slug": tenant["slug"],
        "name": tenant["name"],
        "accounts": len(accounts),
        "connected": sorted(json.loads(tenant["connected_at"])),
        "rows": dict(rows),
        "distinct": dict(distinct),
        "total": total,
        "per_account": [int(np.percentile(counts, q)) for q in (5, 50, 95)] if len(counts) else [],
        "thin_share": float((counts < THIN).mean()) if len(counts) else 0.0,
        "lag": {
            s: [round(float(np.percentile(v, q)), 1) for q in (50, 95, 99)] for s, v in lags.items()
        },
        "versions": {s: dict(sorted(c.items())) for s, c in versions.items()},
        "replay_rate": {s: (rows[s] - distinct[s]) / distinct[s] for s in distinct if distinct[s]},
        "resent_snapshots": resent,
        "quarantine": dict(quarantine),
        "quarantine_rate": sum(quarantine.values()) / total if total else 0.0,
        "renewals": {y: dict(c) for y, c in sorted(renewals.items())},
    }


def build_report(data: str | Path) -> list[dict]:
    data = Path(data)

    def read(name: str) -> list[dict]:
        with (data / name).open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    tenants, accounts, xwalk = read("tenants.csv"), read("accounts.csv"), read("crosswalk.csv")
    out = []
    for t in sorted(tenants, key=lambda t: t["slug"]):
        mine = [a for a in accounts if a["tenant_id"] == t["tenant_id"]]
        out.append(
            tenant_report(data, t, mine, [x for x in xwalk if x["tenant_id"] == t["tenant_id"]])
        )
    return out


def render(report: list[dict]) -> str:
    lines = []
    for r in report:
        lines.append(
            f"== {r['slug']} {r['name']}: {r['accounts']:,} accounts, {r['total']:,} events"
        )
        lines.append(f"   connected: {', '.join(r['connected'])}")
        lines.append(
            "   events per account p5/p50/p95: "
            + "/".join(str(x) for x in r["per_account"])
            + f"; fewer than {THIN} events: {r['thin_share']:.1%}"
        )
        lines.append("   source       events  replays  versions        lag hours p50/p95/p99")
        for s in SOURCES:
            if s not in r["distinct"]:
                continue
            vers = " ".join(f"v{k}:{v}" for k, v in r["versions"][s].items())
            lag = "/".join(str(x) for x in r["lag"][s])
            lines.append(
                f"   {s:<11} {r['distinct'][s]:>7,}  {r['replay_rate'][s]:>6.2%}  {vers:<15} {lag}"
            )
        lines.append(f"   zendesk snapshots re-sent unchanged: {r['resent_snapshots']:,}")
        q = ", ".join(f"{k} {v:,}" for k, v in sorted(r["quarantine"].items())) or "none"
        lines.append(f"   quarantine: {r['quarantine_rate']:.2%} ({q})")
        for year, c in r["renewals"].items():
            n = sum(c.values())
            lines.append(
                f"   renewals {year}: {n:,}, lost {c.get('lost', 0)} ({c.get('lost', 0) / n:.1%}),"
                f" contracted {c.get('contracted', 0)}, renewed {c.get('renewed', 0)}"
            )
        if not r["renewals"]:
            lines.append("   renewals: none observed")
        lines.append("")

    total = Counter()
    for r in report:
        total.update(r["distinct"])
    grand = sum(total.values())
    lines.append(
        f"== all tenants: {sum(r['accounts'] for r in report):,} accounts, {grand:,} events"
    )
    for s in SOURCES:
        share = total[s] / grand if grand else 0.0
        target = TARGET_MIX[s]
        lines.append(
            f"   {s:<11} {total[s]:>7,}  {share:6.1%}  (target {target:.0%}, off by"
            f" {abs(share - target) / target:.0%})"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app data-report")
    parser.add_argument("--data", default="data")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv[1:])
    report = build_report(args.data)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report), end="")
    return 0
