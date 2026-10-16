"""Run configuration: one YAML file holds the seed, the tenants and every date.

Nothing reads the wall clock. Anything that needs "now" takes it from here (the
`train_as_of` and `eval_as_of` dates) or from an `--as-of` argument.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import yaml

from app.ids import stable_uuid
from app.vocab import SOURCES


def parse_utc(value: str | date | datetime) -> datetime:
    """Parse an ISO 8601 value into an aware UTC datetime. A naive value is read as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class TenantConfig:
    slug: str
    name: str
    segment: str
    accounts: int
    base_churn: float
    history_months: int
    connected_at: dict[str, datetime]  # a source that is absent was never connected
    top_k: int
    contraction_threshold: float
    tenant_id: UUID
    accounts_per_csm: int = 120


@dataclass(frozen=True)
class Config:
    name: str
    seed: int
    horizon_start: datetime
    horizon_end: datetime
    train_as_of: datetime
    eval_as_of: datetime
    anchor_warmup_weeks: int
    tenants: tuple[TenantConfig, ...]
    generator: dict = field(default_factory=dict)
    sha256: str = ""


def _require_monday_midnight(label: str, value: datetime) -> None:
    if value.weekday() != 0 or (value.hour, value.minute, value.second) != (0, 0, 0):
        raise ValueError(f"{label} must fall on a Monday at 00:00 UTC, got {value.isoformat()}")


def load_config(path: str | Path) -> Config:
    """Read and validate a config file. The hash covers the file with LF line endings."""
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    doc = yaml.safe_load(raw)
    dates = {k: parse_utc(v) for k, v in doc["dates"].items()}
    defaults = doc.get("defaults", {})
    name, seed = str(doc["name"]), int(doc["seed"])

    tenants = []
    for entry in doc["tenants"]:
        merged = {**defaults, **entry}
        connected = {src: parse_utc(ts) for src, ts in merged.get("sources", {}).items()}
        unknown = set(connected) - set(SOURCES)
        if unknown:
            raise ValueError(f"tenant {merged['slug']}: unknown sources {sorted(unknown)}")
        tenants.append(
            TenantConfig(
                slug=str(merged["slug"]),
                name=str(merged["name"]),
                segment=str(merged["segment"]),
                accounts=int(merged["accounts"]),
                base_churn=float(merged["base_churn"]),
                history_months=int(merged["history_months"]),
                connected_at=connected,
                top_k=int(merged["top_k"]),
                contraction_threshold=float(merged["contraction_threshold"]),
                tenant_id=stable_uuid(name, seed, "tenant", merged["slug"]),
                accounts_per_csm=int(merged["accounts_per_csm"]),
            )
        )
    if len({t.slug for t in tenants}) != len(tenants):
        raise ValueError("tenant slugs must be unique")

    cfg = Config(
        name=name,
        seed=seed,
        horizon_start=dates["horizon_start"],
        horizon_end=dates["horizon_end"],
        train_as_of=dates["train_as_of"],
        eval_as_of=dates["eval_as_of"],
        anchor_warmup_weeks=int(doc.get("model", {}).get("anchor_warmup_weeks", 12)),
        tenants=tuple(tenants),
        generator=doc.get("generator", {}),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    _require_monday_midnight("train_as_of", cfg.train_as_of)
    _require_monday_midnight("eval_as_of", cfg.eval_as_of)
    if not cfg.horizon_start < cfg.train_as_of <= cfg.horizon_end:
        raise ValueError("dates must satisfy horizon_start < train_as_of <= horizon_end")
    return cfg
