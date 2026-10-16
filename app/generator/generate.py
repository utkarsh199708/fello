"""Write the synthetic dataset: reference CSVs, one events file per tenant, truth, manifest.

Reproducibility rules (docs/PLAN.md section 5): every random draw comes from the config seed,
files are opened in binary and written as UTF-8 with LF line endings, and every file is
sorted, so two runs on any machine give the same bytes. No wall-clock read happens here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from pathlib import Path

from app.config import Config
from app.generator import timeutil as tu
from app.generator.events import EMITTERS
from app.generator.world import World, build_world, stream
from app.ids import stable_uuid
from app.vocab import NATIVE_KEY, SOURCES

# A recognisable prefix per source, so an id in a log says where it came from.
EVENT_ID_PREFIX = {
    "salesforce": "sfe_",
    "zendesk": "zde_",
    "product": "pre_",
    "stripe": "evt_",
    "gmail": "gme_",
}


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _event_rows(w: World) -> tuple[list[dict], dict[str, int]]:
    """All raw events of one tenant, with ids and received_at, sorted by arrival."""
    cfg, lag_knobs = w.cfg, w.cfg.generator["lag"]
    lag_rng = stream(cfg, w.tindex, "lag")
    rows, counts = [], {}
    for source in SOURCES:
        if source not in w.tenant.connected_at:
            continue
        raw = EMITTERS[source](w)
        knobs = lag_knobs[source]
        # Typical lag is exponential; a few events arrive days late (the "late tail").
        lag = lag_rng.exponential(knobs["mean_hours"] * 3600, len(raw))
        late = lag_rng.random(len(raw)) < knobs["tail_prob"]
        lag += late * lag_rng.uniform(*knobs["tail_days"], len(raw)) * 86400
        for n, (occurred, event_type, payload) in enumerate(raw):
            digest = hashlib.sha1(f"{w.tenant.slug}|{source}|{n}".encode()).hexdigest()[:24]
            rows.append(
                {
                    "source": source,
                    "source_event_id": EVENT_ID_PREFIX[source] + digest,
                    "source_schema_version": 1,
                    "event_type": event_type,
                    "occurred_at": tu.iso(occurred),
                    "received_at": tu.iso(occurred + int(lag[n])),
                    "payload": payload,
                }
            )
        counts[source] = len(raw)
    # ISO strings of equal length sort the same as the instants they name.
    rows.sort(key=lambda r: (r["received_at"], r["source"], r["source_event_id"]))
    return rows, counts


def _jsonl_bytes(rows: list[dict]) -> bytes:
    lines = (json.dumps(r, separators=(",", ":"), ensure_ascii=True) for r in rows)
    return ("\n".join(lines) + "\n").encode("utf-8") if rows else b""


def generate(cfg: Config, out_dir: str | Path) -> dict:
    """Generate every file under `out_dir` and return the manifest (also written to disk)."""
    out = Path(out_dir)
    for sub in ("events", "truth"):  # the generator owns these folders; start from clean
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {}
    tenants, csms, accounts, contracts, crosswalk = [], [], [], [], []
    health_rows, renewal_rows, unobserved_rows = [], [], []
    counts: dict[str, dict[str, int]] = {}
    stats: dict[str, dict[str, int]] = {}

    # Files list tenants by slug. Random streams are keyed by the tenant's position in the
    # config, so reordering the YAML is the only edit that changes a tenant's data.
    for position, tcfg in sorted(enumerate(cfg.tenants), key=lambda p: p[1].slug):
        world = build_world(cfg, position)
        tid = str(tcfg.tenant_id)
        connected = {s: tu.iso(tu.to_epoch(ts)) for s, ts in sorted(tcfg.connected_at.items())}
        tenants.append(
            [tid, tcfg.slug, tcfg.name, tcfg.segment, tcfg.top_k, tcfg.contraction_threshold]
            + [json.dumps(connected, separators=(",", ":"))]
        )
        for c in world.csms:
            csms.append([c.csm_id, tid, c.name, c.email])
        for a in world.accounts:
            csm_id = world.csms[a.csm].csm_id if a.csm is not None else ""
            accounts.append(
                [a.account_id, tid, a.name, a.domain, csm_id, tu.iso(tu.to_epoch(a.start))]
            )
            for c in a.contracts:
                contracts.append(
                    [
                        stable_uuid(cfg.name, cfg.seed, "contract", tcfg.slug, a.index, c.cycle),
                        tid,
                        a.account_id,
                        c.cycle,
                        c.start.strftime("%Y-%m-%d"),
                        c.end.strftime("%Y-%m-%d"),
                        f"{c.arr:.2f}",
                    ]
                )
            for source, key in (
                ("salesforce", a.salesforce_id),
                ("stripe", a.stripe_id),
                ("zendesk", a.zendesk_org),
                ("product", a.workspace_id),
            ):
                if source in NATIVE_KEY and source in tcfg.connected_at:
                    crosswalk.append([tid, source, key, a.account_id])
            for r in a.renewals:
                renewal_rows.append(
                    [tid, a.account_id, r.cycle, r.date.strftime("%Y-%m-%d"), r.outcome]
                    + [f"{r.prior_arr:.2f}", f"{r.renewed_arr:.2f}"]
                )
            unobserved_rows.append([tid, a.account_id, f"{world.unobserved[a.index]:.4f}"])
            for m in range(world.months):
                live = world.start_epoch[a.index] < world.month_epoch[m + 1] and (
                    world.end_epoch[a.index] > world.month_epoch[m]
                )
                if live:
                    health_rows.append(
                        [tid, a.account_id, world.month_label(m), f"{world.health[a.index, m]:.4f}"]
                    )

        rows, counts[tcfg.slug] = _event_rows(world)
        files[f"events/{tcfg.slug}.jsonl"] = _jsonl_bytes(rows)
        renewals = [r for a in world.accounts for r in a.renewals]
        stats[tcfg.slug] = {
            "accounts": len(world.accounts),
            "csms": len(world.csms),
            "renewals": len(renewals),
            "churned": sum(r.outcome == "churned" for r in renewals),
            "contracted": sum(r.outcome == "contracted" for r in renewals),
        }

    files["tenants.csv"] = _csv_bytes(
        ["tenant_id", "slug", "name", "segment", "top_k", "contraction_threshold", "connected_at"],
        tenants,
    )
    files["csms.csv"] = _csv_bytes(["csm_id", "tenant_id", "name", "email"], csms)
    files["accounts.csv"] = _csv_bytes(
        ["account_id", "tenant_id", "name", "website_domain", "csm_id", "started_at"], accounts
    )
    files["contracts.csv"] = _csv_bytes(
        ["contract_id", "tenant_id", "account_id", "cycle_number", "start_date", "end_date", "arr"],
        contracts,
    )
    files["crosswalk.csv"] = _csv_bytes(
        ["tenant_id", "source", "external_id", "account_id"], crosswalk
    )
    files["truth/health_monthly.csv"] = _csv_bytes(
        ["tenant_id", "account_id", "month", "health"], health_rows
    )
    files["truth/renewal_outcomes.csv"] = _csv_bytes(
        [
            "tenant_id",
            "account_id",
            "cycle_number",
            "renewal_date",
            "outcome",
            "prior_arr",
            "renewed_arr",
        ],
        renewal_rows,
    )
    files["truth/unobserved_factor.csv"] = _csv_bytes(
        ["tenant_id", "account_id", "unobserved_factor"], unobserved_rows
    )

    for name, data in files.items():
        (out / name).write_bytes(data)

    manifest = {
        "config_name": cfg.name,
        "seed": cfg.seed,
        "config_sha256": cfg.sha256,
        "counts": counts,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    (out / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    manifest["stats"] = stats  # for the CLI to print; not written to disk
    return manifest
