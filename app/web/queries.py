"""Every SQL statement the web app runs, as plain functions.

Each takes a connection opened by ``tenant_tx`` as the ``app`` role, so row-level security limits
every query to one tenant. An id from another tenant therefore finds no row, and the route turns
that into a 404. The exceptions are ``tenants`` and ``model_versions``, which have no RLS (the
directory and the model registry); they are read on a plain connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.ml.feature_spec import FEATURE_EVENTS, FEATURES

EVENT_LIMIT = 100


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def _row(conn: Connection, sql: str, **params: Any) -> dict[str, Any] | None:
    r = conn.execute(sa.text(sql), params).mappings().first()
    return dict(r) if r else None


# ---- directory (no RLS) ----------------------------------------------------------------------


def list_tenants(conn: Connection) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT tenant_id, slug, name, segment FROM tenants ORDER BY slug")


def tenant_exists(conn: Connection, tenant_id: uuid.UUID) -> bool:
    return _row(conn, "SELECT 1 AS x FROM tenants WHERE tenant_id = :t", t=tenant_id) is not None


def active_model_version(conn: Connection) -> str | None:
    r = _row(conn, "SELECT model_version FROM model_versions WHERE is_active")
    return r["model_version"] if r else None


# ---- tenant-scoped ---------------------------------------------------------------------------


def tenant_name(conn: Connection) -> str | None:
    """Name of the tenant the transaction is scoped to."""
    r = _row(
        conn,
        "SELECT name FROM tenants WHERE tenant_id = current_setting('app.tenant_id')::uuid",
    )
    return r["name"] if r else None


def list_csms(conn: Connection) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT csm_id, name, email FROM csms ORDER BY name")


def get_csm(conn: Connection, csm_id: uuid.UUID) -> dict[str, Any] | None:
    return _row(conn, "SELECT csm_id, name, email FROM csms WHERE csm_id = :c", c=csm_id)


def latest_as_of(conn: Connection) -> datetime | None:
    return conn.execute(sa.text("SELECT max(as_of) FROM scores")).scalar_one()


def ranked_list(conn: Connection, csm_id: uuid.UUID, as_of: datetime) -> list[dict[str, Any]]:
    """The CSM's book at ``as_of``, best first. One row per account."""
    return _rows(
        conn,
        """
        SELECT s.score_id, s.account_id, a.name AS account, s.rank_in_book, s.probability,
               s.arr, s.days_to_renewal, s.rank_score
        FROM scores s JOIN accounts a USING (account_id)
        WHERE s.csm_id = :c AND s.as_of = :d
        ORDER BY s.rank_in_book, a.name
        """,
        c=csm_id,
        d=as_of,
    )


def get_score(conn: Connection, score_id: uuid.UUID) -> dict[str, Any] | None:
    return _row(
        conn,
        """
        SELECT s.score_id, s.account_id, s.as_of, s.model_version, s.probability, s.margin,
               s.expected_value, s.contributions, s.arr, s.days_to_renewal, s.rank_score,
               s.rank_in_book, a.name AS account, n.features AS feature_values,
               n.ingest_watermark, n.kind AS snapshot_kind
        FROM scores s
        JOIN accounts a USING (account_id)
        JOIN snapshots n ON n.snapshot_id = s.snapshot_id
        WHERE s.score_id = :s
        """,
        s=score_id,
    )


def feature_events(
    conn: Connection, score: dict[str, Any], feature: str
) -> tuple[list[dict[str, Any]], int] | None:
    """Raw events behind one feature of one score, newest first, plus the total count.

    Returns None when ``feature`` is not a known feature. The predicate is the one the feature was
    computed with: same sources and types, window on ``occurred_at``, and only events received by
    the snapshot's ingest watermark. A feature that comes from the account or contract row has no
    events, so it returns an empty list.
    """
    if feature not in FEATURES:
        return None
    spec = FEATURE_EVENTS[feature]
    watermark = score["ingest_watermark"]
    if spec is None or watermark is None:
        return [], 0
    rows = _rows(
        conn,
        """
        SELECT n.occurred_at, n.received_at, n.source, n.event_type, n.match_method,
               r.payload, count(*) OVER () AS total
        FROM normalised_events n JOIN raw_events r ON r.raw_event_id = n.raw_event_id
        WHERE n.account_id = :acc
          AND n.source = ANY(:sources) AND n.event_type = ANY(:types)
          AND n.occurred_at <= :as_of
          AND (CAST(:w AS int) IS NULL OR n.occurred_at > :as_of - make_interval(days => :w))
          AND n.received_at <= :wm
        ORDER BY n.occurred_at DESC
        LIMIT :lim
        """,
        acc=score["account_id"],
        sources=list(spec.sources),
        types=list(spec.event_types),
        as_of=score["as_of"],
        w=spec.window_days,
        wm=watermark,
        lim=EVENT_LIMIT,
    )
    total = rows[0]["total"] if rows else 0
    return rows, total


def get_account(conn: Connection, account_id: uuid.UUID) -> dict[str, Any] | None:
    return _row(
        conn,
        """
        SELECT a.account_id, a.name, a.website_domain, a.started_at, c.name AS csm
        FROM accounts a LEFT JOIN csms c USING (csm_id)
        WHERE a.account_id = :a
        """,
        a=account_id,
    )


def account_contracts(conn: Connection, account_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT cycle_number, start_date, end_date, arr FROM contracts "
        "WHERE account_id = :a ORDER BY cycle_number DESC",
        a=account_id,
    )


def account_scores(conn: Connection, account_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT score_id, as_of, probability, rank_in_book, model_version FROM scores "
        "WHERE account_id = :a ORDER BY as_of DESC LIMIT 20",
        a=account_id,
    )


def account_timeline(
    conn: Connection, account_id: uuid.UUID, received_by: datetime
) -> list[dict[str, Any]]:
    """Events the system had seen by ``received_by``, newest first."""
    return _rows(
        conn,
        """
        SELECT n.occurred_at, n.source, n.event_type, n.match_method, r.payload
        FROM normalised_events n JOIN raw_events r ON r.raw_event_id = n.raw_event_id
        WHERE n.account_id = :a AND n.received_at <= :by
        ORDER BY n.occurred_at DESC
        LIMIT 200
        """,
        a=account_id,
        by=received_by,
    )


# ---- health ----------------------------------------------------------------------------------


def account_count(conn: Connection) -> int:
    return conn.execute(sa.text("SELECT count(*) FROM accounts")).scalar_one()


def last_job_run(conn: Connection) -> dict[str, Any] | None:
    return _row(
        conn,
        "SELECT job_name, run_date, status, trigger, started_at, finished_at "
        "FROM job_runs ORDER BY started_at DESC LIMIT 1",
    )
