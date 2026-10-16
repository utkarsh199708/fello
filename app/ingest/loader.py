"""Load the generated files into Postgres.

Two steps, both safe to run again:

- ``load_reference`` copies the five reference CSVs (tenants, csms, accounts, contracts,
  crosswalk). This stands in for a CRM sync in Step 1 (docs/decisions/step-1-design.md 2.4).
- ``load_events`` copies one tenant's ``events/<slug>.jsonl`` into ``raw_events`` exactly as
  received. Nothing is parsed, matched or dropped here: that is the resolve step's job.

Every tenant's rows go in through ``tenant_tx``, so the row-level-security policy checks each
row's ``tenant_id`` against the tenant being loaded. A wrong id in a file is an error, not a leak.
"""

from __future__ import annotations

import csv
import itertools
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from app.db import copy_insert, get_engine, tenant_tx

BATCH_SIZE = 10_000  # rows per COPY; keeps memory flat for a 70k-event tenant
DO_NOTHING = "ON CONFLICT DO NOTHING"

TENANT_COLUMNS = (
    "tenant_id",
    "slug",
    "name",
    "segment",
    "top_k",
    "contraction_threshold",
    "connected_at",  # JSON text; Postgres parses it into jsonb
)

# Tenant-scoped reference tables in foreign-key order (accounts point at csms). The CSV header
# of each file is the column list, so the files and the tables cannot drift apart silently.
REFERENCE_TABLES = (
    ("csms", "csms.csv"),
    ("accounts", "accounts.csv"),
    ("contracts", "contracts.csv"),
    ("crosswalk", "crosswalk.csv"),
)

RAW_COLUMNS = (
    "tenant_id",
    "source",
    "source_event_id",
    "source_schema_version",
    "event_type",
    "occurred_at",
    "received_at",
    "payload",
)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _by_tenant(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["tenant_id"]].append(row)
    return grouped


def _as_row(row: dict[str, str], columns: Iterable[str]) -> list[str | None]:
    """An empty CSV field means NULL (an unassigned CSM, a CRM account without a website)."""
    return [row[c] if row[c] != "" else None for c in columns]


def _select_tenants(rows: list[dict[str, str]], tenant: str | UUID | None) -> list[dict[str, str]]:
    """Keep the tenants.csv rows named by a slug or a UUID; all of them when ``tenant`` is None."""
    if tenant is None:
        return rows
    wanted = str(tenant).lower()
    chosen = [r for r in rows if wanted in (r["slug"].lower(), r["tenant_id"].lower())]
    if not chosen:
        raise ValueError(f"tenant {tenant!r} is not in tenants.csv")
    return chosen


def load_reference(data_dir: str | Path, tenant: str | UUID | None = None) -> dict[str, int]:
    """Insert tenants, then each tenant's csms, accounts, contracts and crosswalk.

    Returns rows inserted per table. ``ON CONFLICT DO NOTHING`` makes a second run insert 0.
    """
    data_dir = Path(data_dir)
    _, tenant_rows = _read_csv(data_dir / "tenants.csv")
    tenant_rows = _select_tenants(tenant_rows, tenant)
    counts = {"tenants": 0, **{table: 0 for table, _ in REFERENCE_TABLES}}

    # `tenants` is the directory read before any tenant context exists, so it has no RLS and is
    # written outside tenant_tx. It holds no customer data.
    with get_engine("app").begin() as conn:
        counts["tenants"] = copy_insert(
            conn,
            "tenants",
            TENANT_COLUMNS,
            (_as_row(r, TENANT_COLUMNS) for r in tenant_rows),
            on_conflict=DO_NOTHING,
        )

    tables = {}
    for table, filename in REFERENCE_TABLES:
        header, rows = _read_csv(data_dir / filename)
        tables[table] = (header, _by_tenant(rows))

    for trow in tenant_rows:
        tid = trow["tenant_id"]
        with tenant_tx(tid) as conn:
            for table, _ in REFERENCE_TABLES:
                header, grouped = tables[table]
                counts[table] += copy_insert(
                    conn,
                    table,
                    header,
                    (_as_row(r, header) for r in grouped.get(tid, [])),
                    on_conflict=DO_NOTHING,
                )
    return counts


def _event_rows(path: Path, tenant_id: UUID) -> Iterator[tuple]:
    """Yield one raw_events row per line of a JSONL file. The payload is kept whole as JSON text."""
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                yield (
                    tenant_id,
                    e["source"],
                    e["source_event_id"],
                    e["source_schema_version"],
                    e["event_type"],
                    e["occurred_at"],
                    e["received_at"],
                    json.dumps(e["payload"], separators=(",", ":"), ensure_ascii=False),
                )
            except (ValueError, KeyError) as exc:
                raise ValueError(f"{path.name} line {number}: bad event ({exc!r})") from exc


def _batches(rows: Iterable[tuple], size: int) -> Iterator[list[tuple]]:
    it = iter(rows)
    while batch := list(itertools.islice(it, size)):
        yield batch


def load_events(data_dir: str | Path, tenant_id: UUID | str, batch_size: int = BATCH_SIZE) -> int:
    """Load one tenant's events into ``raw_events`` in one transaction. Returns rows inserted.

    A tenant that already has raw events is skipped (returns 0): ``raw_events`` has no unique key,
    because a duplicate delivery is itself a raw fact (Step 3 removes it), so the only guard
    against loading a file twice is this check. One transaction means a bad line leaves nothing
    behind, and the advisory lock stops two loaders from both passing the check at once.
    """
    tid = UUID(str(tenant_id))
    with tenant_tx(tid) as conn:
        slug = conn.execute(
            sa.text("SELECT slug FROM tenants WHERE tenant_id = :tid"), {"tid": tid}
        ).scalar_one()
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext('load_events'), hashtext(:tid))"),
            {"tid": str(tid)},
        )
        already = conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM raw_events WHERE tenant_id = :tid)"), {"tid": tid}
        ).scalar_one()
        if already:
            return 0
        path = Path(data_dir) / "events" / f"{slug}.jsonl"
        return sum(
            copy_insert(conn, "raw_events", RAW_COLUMNS, batch)
            for batch in _batches(_event_rows(path, tid), batch_size)
        )


def load_all(data_dir: str | Path, tenant: str | UUID | None = None) -> dict:
    """Load the reference tables, then every selected tenant's events.

    Returns ``{"reference": {table: rows}, "events": {slug: rows}}``.
    """
    reference = load_reference(data_dir, tenant)
    _, tenant_rows = _read_csv(Path(data_dir) / "tenants.csv")
    events = {
        r["slug"]: load_events(data_dir, r["tenant_id"])
        for r in _select_tenants(tenant_rows, tenant)
    }
    return {"reference": reference, "events": events}
