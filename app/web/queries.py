"""Every SQL statement the web app runs, as plain functions.

Each takes a connection opened by ``tenant_tx`` as the ``app`` role, so row-level security limits
every query to one tenant. An id from another tenant therefore finds no row, and the route turns
that into a 404. The exceptions are ``tenants`` and ``model_versions``, which have no RLS (the
directory and the model registry); they are read on a plain connection.
"""

from __future__ import annotations

import json
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


UNASSIGNED = "unassigned"  # session value for the virtual owner of accounts with no CSM


SORTS = {
    "rank_score": "s.rank_score DESC",
    "probability": "s.probability DESC",
    "arr": "s.arr DESC",
    "days_to_renewal": "s.days_to_renewal ASC",
}


def ranked_list(
    conn: Connection, csm_id: uuid.UUID | str, as_of: datetime, sort: str = "rank_score"
) -> list[dict[str, Any]]:
    """The CSM's book at ``as_of``. Escalated tasks first, then by ``sort`` (task 6.1).

    ``csm_id == UNASSIGNED`` selects the book of accounts that have no CSM (csm_id NULL). Each row
    carries its active task (open, snoozed or escalated), and its rank on the first night of the
    week, for the since-Monday markers (task 6.2). Thin accounts (``evidence_state =
    'insufficient'``) are included; the page lists them separately.
    """
    owner = "s.csm_id IS NULL" if csm_id == UNASSIGNED else "s.csm_id = :c"
    order = SORTS.get(sort, SORTS["rank_score"])
    return _rows(
        conn,
        f"""
        WITH monday AS (
            SELECT min(as_of) AS as_of FROM scores
            WHERE as_of >= date_trunc('week', CAST(:d AS timestamptz))
        )
        SELECT s.score_id, s.account_id, a.name AS account, s.rank_in_book, s.probability,
               s.arr, s.days_to_renewal, s.rank_score, s.colour, s.evidence_state, s.k,
               (s.coverage->>'total_event_count')::int AS event_count,
               t.task_id, t.status AS task_status, t.run_date AS task_run_date,
               t.rule_fired, b.text AS briefing,
               m.rank_in_book AS monday_rank, m.score_id IS NOT NULL AS seen_monday,
               (SELECT as_of FROM monday) AS monday_as_of
        FROM scores s
        JOIN accounts a USING (account_id)
        LEFT JOIN LATERAL (
            SELECT task_id, status, run_date, rule_fired, briefing_id FROM tasks
            WHERE tasks.account_id = s.account_id AND status IN ('open', 'snoozed', 'escalated')
            ORDER BY run_date DESC LIMIT 1
        ) t ON true
        LEFT JOIN briefings b ON b.briefing_id = t.briefing_id
        LEFT JOIN scores m ON m.account_id = s.account_id AND m.model_version = s.model_version
             AND m.as_of = (SELECT as_of FROM monday)
        WHERE {owner} AND s.as_of = :d
        ORDER BY (t.status = 'escalated') DESC NULLS LAST, {order}, a.name
        """,
        d=as_of,
        **({} if csm_id == UNASSIGNED else {"c": csm_id}),
    )


def marker(row: dict[str, Any]) -> str:
    """Since-Monday marker: new in the top k, moved up, moved down, or nothing."""
    rank, before, k = row["rank_in_book"], row["monday_rank"], row["k"]
    if rank is None or row["monday_as_of"] is None:
        return ""
    if rank <= k and (before is None or before > k):
        return "new"
    if before is None:
        return ""
    if rank < before:
        return "moved up"
    return "moved down" if rank > before else ""


def get_score(conn: Connection, score_id: uuid.UUID) -> dict[str, Any] | None:
    return _row(
        conn,
        """
        SELECT s.score_id, s.account_id, s.as_of, s.model_version, s.probability, s.margin,
               s.expected_value, s.contributions, s.arr, s.days_to_renewal, s.rank_score,
               s.rank_in_book, s.colour, s.evidence_state, s.k, s.coverage,
               a.name AS account, n.features AS feature_values,
               n.ingest_watermark, n.kind AS snapshot_kind,
               (SELECT count(*) FROM normalised_events e
                WHERE e.account_id = s.account_id AND e.occurred_at <= s.as_of
                  AND (n.ingest_watermark IS NULL OR e.received_at > n.ingest_watermark))
                   AS arrived_after,
               (SELECT text FROM briefings b WHERE b.score_id = s.score_id) AS briefing
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


def account_contracts(
    conn: Connection, account_id: uuid.UUID, as_of: datetime
) -> list[dict[str, Any]]:
    """Cycles that the system knew had started by ``as_of``.

    contracts.csv runs ahead of time (it holds cycles up to the horizon), so showing a later
    cycle would tell a CSM whether, and at what ARR, a renewal will happen before it has. A
    cycle after the first shows only once the Salesforce renewal event that opened it had been
    received, the same rule as ``app.ml.features.active_contracts``.
    """
    return _rows(
        conn,
        """
        SELECT c.cycle_number, c.start_date, c.end_date, c.arr
        FROM contracts c
        WHERE c.account_id = :a AND c.start_date <= CAST(:as_of AS date)
          AND (c.cycle_number = 1 OR EXISTS (
                SELECT 1 FROM normalised_events e
                WHERE e.account_id = c.account_id
                  AND e.source = 'salesforce' AND e.event_type = 'opportunity_stage_changed'
                  AND (e.attrs->>'renewal_date')::date = c.start_date
                  AND e.received_at <= :as_of))
        ORDER BY c.cycle_number DESC
        """,
        a=account_id,
        as_of=as_of,
    )


def account_scores(conn: Connection, account_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT score_id, as_of, probability, rank_in_book, colour, evidence_state,"
        " model_version FROM scores WHERE account_id = :a ORDER BY as_of DESC LIMIT 20",
        a=account_id,
    )


def account_tasks(conn: Connection, account_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every task of the account, newest first, with its audit trail."""
    tasks = _rows(
        conn,
        "SELECT t.task_id, t.run_date, t.status, t.rule_fired, t.queue, t.probability,"
        " t.snoozed_until, t.dismissed_reason, t.escalated_at, b.text AS briefing"
        " FROM tasks t LEFT JOIN briefings b ON b.briefing_id = t.briefing_id"
        " WHERE t.account_id = :a ORDER BY t.run_date DESC",
        a=account_id,
    )
    for t in tasks:
        t["events"] = _rows(
            conn,
            "SELECT kind, detail, created_at FROM task_events WHERE task_id = :t"
            " ORDER BY task_event_id",
            t=t["task_id"],
        )
    return tasks


TASK_ACTIONS = {"done", "snoozed", "dismissed"}
SNOOZE_DAYS = 7


def set_task_status(
    conn: Connection, task_id: uuid.UUID, status: str, reason: str | None, by: str
) -> bool:
    """A CSM's action on a task (task 6.4): done, snooze for a week, or dismiss with a reason.

    Only an active task can change, and every change writes an audit row. A dismissal records
    the account's latest probability, which suppresses re-tasking until it rises past it
    (``app.jobs.policy``). Returns False when the task is not visible or not active.
    """
    task = _row(
        conn,
        "SELECT t.task_id, t.status, t.account_id,"
        " (SELECT probability FROM scores s WHERE s.account_id = t.account_id"
        "  ORDER BY as_of DESC LIMIT 1) AS latest_probability"
        " FROM tasks t WHERE t.task_id = :t AND t.status IN ('open', 'snoozed', 'escalated')",
        t=task_id,
    )
    if task is None or status not in TASK_ACTIONS:
        return False
    conn.execute(
        sa.text(
            """
            UPDATE tasks SET status = :st, updated_at = now(),
                   snoozed_until = CASE WHEN :st = 'snoozed'
                                        THEN CURRENT_DATE + CAST(:days AS int) END,
                   dismissed_reason = CASE WHEN :st = 'dismissed' THEN :reason END,
                   dismissed_probability = CASE WHEN :st = 'dismissed' THEN :p END
            WHERE task_id = :t
            """
        ),
        {"st": status, "days": SNOOZE_DAYS, "reason": reason, "p": task["latest_probability"],
         "t": task_id},
    )  # fmt: skip
    conn.execute(
        sa.text(
            "INSERT INTO task_events (tenant_id, task_id, kind, detail)"
            " SELECT tenant_id, task_id, 'status_changed', CAST(:d AS jsonb) FROM tasks"
            " WHERE task_id = :t"
        ),
        {
            "t": task_id,
            "d": json.dumps({"from": task["status"], "to": status, "reason": reason, "by": by}),
        },
    )
    return True


def account_timeline(
    conn: Connection, account_id: uuid.UUID, received_by: datetime
) -> list[dict[str, Any]]:
    """Events the system had seen by ``received_by``, newest first."""
    return _rows(
        conn,
        """
        SELECT n.occurred_at, n.received_at, n.source, n.event_type, n.match_method, r.payload,
               (SELECT min(s.as_of) FROM scores s
                WHERE s.account_id = n.account_id
                  AND s.as_of >= n.occurred_at AND s.as_of < n.received_at) AS late_for
        FROM normalised_events n JOIN raw_events r ON r.raw_event_id = n.raw_event_id
        WHERE n.account_id = :a AND n.received_at <= :by
        ORDER BY n.occurred_at DESC
        LIMIT 200
        """,
        a=account_id,
        by=received_by,
    )


# ---- health ----------------------------------------------------------------------------------


def data_quality(conn: Connection) -> dict[str, Any]:
    """What the scores could not use, for the tenant in context (task 3.5).

    ``quarantined``: events no account could be found for, by reason. ``rejected``: replays and
    payloads no schema version could read, by reason. ``matched``: events that reached the model.
    Row-level security limits every count to the tenant in context.
    """

    def by_reason(table: str) -> dict[str, int]:
        sql = f"SELECT reason, count(*) AS n FROM {table} GROUP BY reason ORDER BY reason"  # noqa: S608
        return {r["reason"]: r["n"] for r in _rows(conn, sql)}

    quarantined, rejected = by_reason("quarantine"), by_reason("rejects")
    matched = conn.execute(sa.text("SELECT count(*) FROM normalised_events")).scalar_one()
    return {
        "matched": matched,
        "quarantined": quarantined,
        "rejected": rejected,
        "quarantined_total": sum(quarantined.values()),
        "rejected_total": sum(rejected.values()),
    }


# ---- runs, staleness, quarantine, misses (tasks 6.7, 6.8) -------------------------------------


def run_history(conn: Connection, limit: int = 30) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT run_id, run_date, as_of, trigger, status, model_version, policy_version,"
        " counts, stage_ms, error, started_at, finished_at FROM job_runs"
        " WHERE job_name = 'nightly' ORDER BY run_date DESC, as_of DESC LIMIT :n",
        n=limit,
    )


def get_run(conn: Connection, run_id: uuid.UUID) -> dict[str, Any] | None:
    run = _row(conn, "SELECT * FROM job_runs WHERE run_id = :r", r=run_id)
    if run is None:
        return None
    run["transitions"] = _row(
        conn,
        "SELECT count(*) AS total, count(*) FILTER (WHERE entered_top_k) AS entered,"
        " count(*) FILTER (WHERE left_top_k) AS left FROM transitions WHERE run_id = :r",
        r=run_id,
    )
    run["tasks"] = _rows(
        conn,
        "SELECT t.task_id, t.account_id, a.name AS account, t.rule_fired, t.status, t.queue"
        " FROM tasks t JOIN accounts a USING (account_id) WHERE t.run_id = :r"
        " ORDER BY a.name",
        r=run_id,
    )
    return run


def staleness(conn: Connection, now: datetime) -> dict[str, Any]:
    """The header every page shows: scores as of when, last run how, and whether that is stale.

    Stale when the newest run did not succeed, or the newest success finished over a day ago.
    A failed night still shows the previous night's scores, with this warning above them.
    """
    last = _row(
        conn,
        "SELECT status, finished_at, run_date FROM job_runs WHERE job_name = 'nightly'"
        " ORDER BY run_date DESC, as_of DESC LIMIT 1",
    )
    success = conn.execute(
        sa.text(
            "SELECT max(finished_at) FROM job_runs WHERE job_name = 'nightly'"
            " AND status = 'succeeded'"
        )
    ).scalar_one()
    scores_as_of = latest_as_of(conn)
    stale = (
        last is None
        or last["status"] != "succeeded"
        or success is None
        or (now - success).total_seconds() > 86400
    )
    return {"scores_as_of": scores_as_of, "last": last, "last_success": success, "stale": stale}


def quarantine_summary(conn: Connection, sample: int = 25) -> dict[str, Any]:
    return {
        "by_reason": _rows(
            conn,
            "SELECT reason, source, count(*) AS n FROM quarantine GROUP BY reason, source"
            " ORDER BY n DESC",
        ),
        "rejects": _rows(
            conn,
            "SELECT reason, source, count(*) AS n FROM rejects GROUP BY reason, source"
            " ORDER BY n DESC",
        ),
        "recent": _rows(
            conn,
            "SELECT q.reason, q.source, q.detail, r.event_type, r.occurred_at, r.received_at"
            " FROM quarantine q JOIN raw_events r USING (raw_event_id)"
            " ORDER BY r.received_at DESC LIMIT :n",
            n=sample,
        ),
    }


def worst_misses(conn: Connection, window_days: int = 90, limit: int = 50) -> list[dict[str, Any]]:
    """Lost renewals the live system scored but never put in the CSM's top k beforehand.

    "Lost" is the Salesforce renewal event (closed_lost) the system has received. Only renewals
    with at least one nightly score in the ``window_days`` before them count: before the first
    deploy there were no scores to miss with. The offline evaluation (``python -m app evaluate``)
    reports misses over the whole holdout instead.
    """
    return _rows(
        conn,
        """
        WITH lost AS (
            SELECT DISTINCT account_id, (attrs->>'renewal_date')::date AS renewal_date
            FROM normalised_events
            WHERE source = 'salesforce' AND event_type = 'opportunity_stage_changed'
              AND attrs->>'stage' = 'closed_lost'
        )
        SELECT l.account_id, a.name AS account, l.renewal_date, max(s.arr) AS arr,
               min(s.rank_in_book) AS best_rank, count(*) AS nights_scored
        FROM lost l
        JOIN accounts a USING (account_id)
        JOIN scores s ON s.account_id = l.account_id
             AND s.as_of < l.renewal_date
             AND s.as_of >= l.renewal_date - make_interval(days => :w)
        GROUP BY l.account_id, a.name, l.renewal_date
        HAVING bool_and(s.rank_in_book IS NULL OR s.rank_in_book > s.k)
        ORDER BY max(s.arr) DESC
        LIMIT :n
        """,
        w=window_days,
        n=limit,
    )


def account_count(conn: Connection) -> int:
    return conn.execute(sa.text("SELECT count(*) FROM accounts")).scalar_one()


def last_job_run(conn: Connection) -> dict[str, Any] | None:
    return _row(
        conn,
        "SELECT job_name, run_date, as_of, status, trigger, started_at, finished_at "
        "FROM job_runs ORDER BY run_date DESC, as_of DESC LIMIT 1",
    )
