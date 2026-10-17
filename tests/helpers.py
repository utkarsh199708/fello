"""Shared test seeding. Import as ``from tests.helpers import load_tiny`` (or ``seed_tiny``).

A test that uses these must request the ``database_url`` fixture first: it migrates the test
database and points ``DATABASE_URL`` at it. Each test module generates into its own ``tmp_path``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from app.config import load_config
from app.db import make_engine, tenant_tx
from app.generator.generate import generate
from app.ingest.loader import load_all
from app.ingest.resolve import resolve

ROOT = Path(__file__).resolve().parent.parent
TINY = ROOT / "tests" / "fixtures" / "tiny.yaml"

# Child tables first, so a purge never trips a foreign key.
PURGE_ORDER = (
    "narrations",
    "task_events",
    "tasks",
    "briefings",
    "transitions",
    "scores",
    "job_runs",
    "snapshots",
    "quarantine",
    "rejects",
    "normalised_events",
    "raw_events",
    "crosswalk",
    "contracts",
    "accounts",
    "csms",
)


def load_tiny(data_dir: Path, config: Path = TINY) -> list[UUID]:
    """Generate the tiny dataset, load it and resolve it at its ``train_as_of``.

    Returns the tenant ids in config order. Idempotent: the generator writes the same bytes, the
    loader skips tenants that already have events, and resolve only handles unprocessed events.
    """
    cfg = load_config(config)
    generate(cfg, data_dir)
    load_all(data_dir)
    ids = [t.tenant_id for t in cfg.tenants]
    for tid in ids:
        with tenant_tx(tid) as conn:
            resolve(conn, tid, cfg.train_as_of)
    return ids


def seed_tiny(data_dir: Path, config: Path = TINY) -> list[UUID]:
    """``load_tiny``, then anchors, training, and a nightly snapshot and scores at ``train_as_of``.

    Returns the tenant ids in config order. Idempotent: anchors, snapshots and scores are keyed,
    and training on the same rows gives the same model version, which is only re-activated.
    Imports the ML modules here so that tests which only load data never import scikit-learn.
    Purge the returned tenants at module teardown: ``tiny-loader`` reuses their slugs.
    """
    from app.ml.features import write_snapshots
    from app.ml.labels import build_anchors
    from app.ml.score import score_tenant
    from app.ml.train import train

    cfg = load_config(config)
    ids = load_tiny(data_dir, config)
    start = cfg.horizon_start + timedelta(weeks=cfg.anchor_warmup_weeks)
    for tid in ids:
        with tenant_tx(tid) as conn:
            build_anchors(conn, tid, start, cfg.train_as_of)
    train(cfg)
    for tid in ids:
        with tenant_tx(tid) as conn:
            write_snapshots(conn, tid, cfg.train_as_of, "nightly")
            score_tenant(conn, tid, cfg.train_as_of)
    return ids


def purge_tenants(admin_url: str, tenant_ids: list[UUID]) -> None:
    """Delete tenants and everything under them, as the admin user.

    The runtime roles cannot DELETE, and a superuser bypasses row-level security. Tests that add
    tenants call this at teardown so later modules in the same session see only their own.
    """
    engine = make_engine(admin_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            for table in PURGE_ORDER:
                conn.execute(
                    sa.text(f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"),  # noqa: S608
                    {"ids": tenant_ids},
                )
            conn.execute(
                sa.text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"), {"ids": tenant_ids}
            )
    finally:
        engine.dispose()
