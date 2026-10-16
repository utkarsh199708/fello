"""Bootstrap: bring an empty database to a scored first night in one idempotent command.

    python -m app bootstrap --config config/full.yaml [--data data]

Eight steps, all at ``as_of = cfg.train_as_of`` (time comes from the config, never the clock).
Each step is safe to run again, so a bootstrap that died half-way is finished by running it again,
and a second run of a finished bootstrap changes nothing:

1. ``migrate``: create the database, roles and schema if missing.
2. ``generate`` the data, unless ``manifest.json`` already records this config's hash.
3. Refuse to go on if the database holds tenants that this config does not name. Mixing two
   datasets would put foreign tenants in the nightly loop and break the slug-unique load.
4. ``load_all``: reference rows ``ON CONFLICT DO NOTHING``; a tenant with events is skipped.
5. ``resolve`` the events received by ``as_of``; already-handled events are skipped.
6. ``build_anchors`` for every Monday from ``horizon_start + warmup`` to ``as_of``; stored anchors
   are skipped by the snapshot key.
7. ``train``: the same rows give the same model version, which is only re-activated.
8. ``run_nightly(as_of, trigger="bootstrap")``: writes the first job_runs row per tenant, and is
   a ``no_op`` once that night has succeeded.

The worker runs this on first start, when ``model_versions`` is empty, so the heavy inserts happen
next to the database rather than row by row from a laptop through a public proxy.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from time import perf_counter

import sqlalchemy as sa

from app.config import Config, load_config
from app.db import get_engine, tenant_tx
from app.generator.generate import generate
from app.ingest.loader import load_all
from app.ingest.resolve import resolve
from app.jobs.nightly import TenantRun, run_nightly
from app.migrate import migrate
from app.ml.labels import build_anchors
from app.ml.train import train


@contextmanager
def step(name: str) -> Iterator[None]:
    """Print how long a step took. ``perf_counter`` is a stopwatch, not a clock read."""
    started = perf_counter()
    print(f"bootstrap: {name} ...", flush=True)
    yield
    print(f"bootstrap: {name} done in {perf_counter() - started:.1f} s", flush=True)


def generated_for(data_dir: Path, cfg: Config) -> bool:
    """True when ``data_dir`` already holds the output of exactly this config file."""
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return False
    return json.loads(manifest.read_text(encoding="utf-8")).get("config_sha256") == cfg.sha256


def foreign_tenants(cfg: Config) -> list[str]:
    """Slugs of tenants in the database that ``cfg`` does not name (``tenants`` has no RLS)."""
    ours = {t.tenant_id for t in cfg.tenants}
    with get_engine("app").connect() as conn:
        rows = conn.execute(sa.text("SELECT tenant_id, slug FROM tenants ORDER BY slug")).all()
    return [r.slug for r in rows if r.tenant_id not in ours]


def bootstrap(config_path: str | Path, data_dir: str | Path = "data") -> list[TenantRun]:
    """Run the eight steps and return the first night's per-tenant results."""
    cfg = load_config(config_path)
    data_dir = Path(data_dir)
    as_of = cfg.train_as_of
    ids = [t.tenant_id for t in cfg.tenants]
    print(f"bootstrap: config {cfg.name} ({config_path}), as_of {as_of.isoformat()}")

    with step("migrate"):
        migrate()

    with step("generate"):
        if generated_for(data_dir, cfg):
            print(f"  {data_dir} already holds this config's data, kept")
        else:
            generate(cfg, data_dir)

    foreign = foreign_tenants(cfg)
    if foreign:
        raise RuntimeError(
            f"database already holds tenants not in {config_path}: {', '.join(foreign)}."
            " Bootstrap into an empty database, or use the config that created them."
        )

    with step("load"):
        loaded = load_all(data_dir)
        print(f"  reference {loaded['reference']}  events {loaded['events']}")

    with step("resolve"):
        for tid in ids:
            with tenant_tx(tid) as conn:
                counts = resolve(conn, tid, as_of)
            print(f"  {tid} normalised={counts.normalised} quarantined={counts.quarantined}")

    with step("anchors"):
        start = cfg.horizon_start + timedelta(weeks=cfg.anchor_warmup_weeks)
        for tid in ids:
            with tenant_tx(tid) as conn:
                written = build_anchors(conn, tid, start, as_of)
            print(f"  {tid} anchor snapshots written={written}")

    with step("train"):
        print(f"  active model {train(cfg)}")

    with step("nightly"):
        runs = run_nightly(as_of, trigger="bootstrap")
        for run in runs:
            print(f"  {run.tenant_id} {run.status} {run.counts} {run.error or ''}".rstrip())
    return runs
