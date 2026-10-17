"""The nightly job's actions, stages 5 to 8: transitions, tasks, escalation, briefings.

Called by ``app.jobs.nightly`` inside the night's ``tenant_tx``, after the data-quality gate has
passed, so nothing here is written for a blocked night. Every write is keyed or guarded, so a
re-run of the same night (a re-claimed failure, or ``--force``) adds nothing new:

- transitions are unique per (account, run);
- tasks are unique per (account, run_date);
- reasons are appended to an active task at most once per run;
- escalation and snooze expiry only move tasks still in the state they start from;
- briefings are unique per score.

``policy.decide`` makes the choices; this module only reads its inputs and applies them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

import numpy as np
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.ids import stable_uuid
from app.jobs.policy import ACTIVE, ANTI_FLAP_RUNS, POLICY_VERSION, Candidate, History, decide
from app.ml.feature_spec import FEATURE_LABELS, FEATURE_SOURCE, PERCENT_FEATURES, REASON_FEATURES

ESCALATE_AFTER_BUSINESS_DAYS = 5
ESCALATE_WITHIN_RENEWAL_DAYS = 60
TOP_REASONS = 5
BRIEFING_TEMPLATE = "reasons-v1"


@dataclass
class ActionCounts:
    transitions: int = 0
    entered_top_k: int = 0
    tasks_created: int = 0
    reasons_appended: int = 0
    budget_exceeded: int = 0
    tasks_escalated: int = 0
    snoozes_ended: int = 0
    briefings: int = 0
    rebaselined: bool = False
    seeded: bool = False
    llm_calls: int = 0  # always 0 here: narration runs after the night (app.jobs.narrate)
    by_rule: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _rows(conn: Connection, sql: str, **params) -> list:
    return conn.execute(sa.text(sql), params).all()


def previous_runs(conn: Connection, run_date: date, n: int) -> list:
    """The tenant's last ``n`` succeeded nights before ``run_date``, newest first."""
    return _rows(
        conn,
        "SELECT run_id, run_date, model_version FROM job_runs"
        " WHERE job_name = 'nightly' AND status = 'succeeded' AND run_date < :d"
        " ORDER BY run_date DESC LIMIT :n",
        d=run_date,
        n=n,
    )


def tonight_scores(conn: Connection, as_of: datetime, model_version: str) -> list:
    return _rows(
        conn,
        "SELECT score_id, account_id, csm_id, rank_in_book, k, probability, rank_score,"
        " days_to_renewal, arr, contributions, snapshot_id FROM scores"
        " WHERE as_of = :as_of AND model_version = :mv ORDER BY account_id",
        as_of=as_of,
        mv=model_version,
    )


def write_transitions(conn, tid, run_id, run_date, scores, prev_run) -> tuple[dict, ActionCounts]:
    """One transition per scored account, against the previous completed night."""
    counts = ActionCounts()
    before = {}
    if prev_run is not None:
        before = {
            r.account_id: r
            for r in _rows(
                conn,
                "SELECT score_id, account_id, rank_in_book, k FROM scores WHERE run_id = :r",
                r=prev_run.run_id,
            )
        }
    ids = {}
    rows = []
    for s in scores:
        b = before.get(s.account_id)
        was = b is not None and b.rank_in_book is not None and b.rank_in_book <= b.k
        now = s.rank_in_book is not None and s.rank_in_book <= s.k
        tid_ = stable_uuid("transition", tid, s.account_id, run_id)
        ids[s.account_id] = tid_
        rows.append(
            {
                "id": tid_,
                "tid": tid,
                "a": s.account_id,
                "run": run_id,
                "d": run_date,
                "fs": b.score_id if b else None,
                "ts": s.score_id,
                "fr": b.rank_in_book if b else None,
                "tr": s.rank_in_book,
                "k": s.k,
                "in": now and not was,
                "out": was and not now,
                "pv": POLICY_VERSION,
            }
        )
        counts.entered_top_k += int(now and not was)
    if rows:
        conn.execute(
            sa.text(
                "INSERT INTO transitions (transition_id, tenant_id, account_id, run_id, run_date,"
                " from_score_id, to_score_id, from_rank, to_rank, k, entered_top_k, left_top_k,"
                " policy_version) VALUES (:id, :tid, :a, :run, :d, :fs, :ts, :fr, :tr, :k, :in,"
                " :out, :pv) ON CONFLICT (tenant_id, account_id, run_id) DO NOTHING"
            ),
            rows,
        )
    counts.transitions = len(rows)
    return ids, counts


def build_history(conn, scores, prev_runs) -> dict[UUID, History]:
    """Top-k membership on the previous nights, and each account's task state."""
    top = {}
    for run in prev_runs[:ANTI_FLAP_RUNS]:
        top[run.run_id] = {
            r.account_id
            for r in _rows(
                conn,
                "SELECT account_id FROM scores WHERE run_id = :r AND rank_in_book <= k",
                r=run.run_id,
            )
        }
    tasks: dict[UUID, list] = {}
    for t in _rows(
        conn,
        "SELECT task_id, account_id, status, probability, dismissed_probability FROM tasks"
        " ORDER BY run_date, created_at",
    ):
        tasks.setdefault(t.account_id, []).append(t)
    history = {}
    for s in scores:
        mine = tasks.get(s.account_id, [])
        active = next((t for t in reversed(mine) if t.status in ACTIVE), None)
        last = mine[-1] if mine else None
        history[s.account_id] = History(
            top_k_previous=tuple(s.account_id in top[r.run_id] for r in prev_runs[:ANTI_FLAP_RUNS]),
            active_task_id=active.task_id if active else None,
            last_task_probability=last.probability if last else None,
            dismissed_probability=(
                last.dismissed_probability
                if last is not None and last.status == "dismissed"
                else None
            ),
        )
    return history


def reasons_of(score, features: dict) -> list[dict]:
    """The deterministic reason list: the features pushing risk up most, with their values."""
    contrib = score.contributions
    up = sorted(
        ((f, v) for f, v in contrib.items() if v > 0 and f in REASON_FEATURES),
        key=lambda fv: -fv[1],
    )
    out = []
    for feature, value in up[:TOP_REASONS]:
        source = FEATURE_SOURCE.get(feature)
        connected = source is None or features.get(f"{source}_connected") != 0
        out.append(
            {
                "feature": feature,
                "label": FEATURE_LABELS.get(feature, feature),
                "value": features.get(feature),
                "contribution": round(float(value), 6),
                "source": source or "account",
                "connected": connected,
            }
        )
    return out


def reason_value_text(r: dict) -> str:
    """One reason's value as the briefing writes it. NULL is "<Source> not connected", never 0."""
    value = r["value"]
    if value is None:
        return f"{r['source'].title()} not connected" if not r["connected"] else "no data yet"
    if r.get("feature") in PERCENT_FEATURES:
        return f"{value:+.0%}" if r["feature"].startswith("usage_change") else f"{value:.0%}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def render_briefing(score, reasons: list[dict]) -> str:
    """Template text from the reason list alone, so the text can claim nothing else.

    A NULL value is written as "<Source> not connected", never as zero.
    """
    head = (
        f"Renewal risk {score.probability:.0%} on {float(score.arr):,.0f} ARR,"
        f" renewal in {score.days_to_renewal} days."
    )
    if not reasons:
        return head + " No single factor stands out; review the account timeline."
    parts = [f"{r['label']}: {reason_value_text(r)}" for r in reasons]
    return head + " What raises the risk most: " + "; ".join(parts) + "."


def apply_actions(
    conn: Connection,
    tid: UUID,
    run_id: UUID,
    as_of: datetime,
    model_version: str,
    *,
    bootstrap_night: bool,
    stage_ms: dict | None = None,
) -> ActionCounts:
    stage_ms = {} if stage_ms is None else stage_ms
    started = perf_counter()
    run_date = as_of.date()
    scores = tonight_scores(conn, as_of, model_version)
    prev = previous_runs(conn, run_date, ANTI_FLAP_RUNS)
    rebaseline = bool(prev) and prev[0].model_version != model_version
    transition_ids, counts = write_transitions(
        conn, tid, run_id, run_date, scores, prev[0] if prev else None
    )
    stage_ms["transitions"] = round((perf_counter() - started) * 1000)
    started = perf_counter()
    counts.rebaselined = rebaseline
    # The first night under this policy version seeds too: a deployment that ran nights before
    # the policy existed (or under an older one) would otherwise never task accounts already red.
    first_policy_night = not conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM job_runs WHERE job_name = 'nightly'"
            " AND status = 'succeeded' AND policy_version = :pv AND run_date < :d)"
        ),
        {"pv": POLICY_VERSION, "d": run_date},
    ).scalar_one()
    counts.seeded = bootstrap_night or rebaseline or not prev or first_policy_night
    if rebaseline:
        conn.execute(
            sa.text(
                "INSERT INTO task_events (tenant_id, run_id, kind, detail)"
                " VALUES (:tid, :run, 'rebaselined', CAST(:d AS jsonb))"
            ),
            {
                "tid": tid,
                "run": run_id,
                "d": json.dumps({"from_model": prev[0].model_version, "to_model": model_version}),
            },
        )

    # Snoozes that have run out come back as open tasks before tonight's decisions.
    ended = _rows(
        conn,
        "UPDATE tasks SET status = 'open', snoozed_until = NULL, updated_at = now()"
        " WHERE status = 'snoozed' AND snoozed_until <= :d RETURNING task_id",
        d=run_date,
    )
    for r in ended:
        _event(conn, tid, r.task_id, run_id, "snooze_ended", {})
    counts.snoozes_ended = len(ended)

    by_account = {s.account_id: s for s in scores}
    candidates = [
        Candidate(
            s.account_id, s.csm_id, s.score_id, s.rank_in_book, s.probability, s.rank_score, s.k
        )
        for s in scores
    ]
    history = build_history(conn, scores, prev)
    plan = decide(candidates, history, seed=counts.seeded)
    features = _features(conn, [by_account[d.account_id].snapshot_id for d in plan.decisions])

    for d in plan.decisions:
        s = by_account[d.account_id]
        reasons = reasons_of(s, features.get(s.snapshot_id, {}))
        entry = {
            "run_id": str(run_id),
            "run_date": run_date.isoformat(),
            "rule": d.rule,
            "reasons": reasons,
        }
        counts.by_rule[d.rule] = counts.by_rule.get(d.rule, 0) + (d.kind == "create")
        if d.kind == "create":
            task_id = stable_uuid("task", tid, d.account_id, run_date)
            briefing_id = stable_uuid("briefing", tid, s.score_id)
            conn.execute(
                sa.text(
                    "INSERT INTO briefings (briefing_id, tenant_id, account_id, score_id, run_id,"
                    " reasons, text, template) VALUES (:b, :tid, :a, :s, :run,"
                    " CAST(:reasons AS jsonb), :text, :tpl)"
                    " ON CONFLICT (tenant_id, score_id) DO NOTHING"
                ),
                {
                    "b": briefing_id,
                    "tid": tid,
                    "a": d.account_id,
                    "s": s.score_id,
                    "run": run_id,
                    "reasons": json.dumps(reasons),
                    "text": render_briefing(s, reasons),
                    "tpl": BRIEFING_TEMPLATE,
                },
            )
            inserted = _rows(
                conn,
                "INSERT INTO tasks (task_id, tenant_id, account_id, run_date, score_id, run_id,"
                " rule_fired, policy_version, csm_id, queue, owner_missing, transition_id,"
                " briefing_id, probability, reasons) VALUES (:t, :tid, :a, :d, :s, :run, :rule,"
                " :pv, :csm, :queue, :missing, :tr, :b, :p, CAST(:reasons AS jsonb))"
                " ON CONFLICT (tenant_id, account_id, run_date) DO NOTHING RETURNING task_id",
                t=task_id,
                tid=tid,
                a=d.account_id,
                d=run_date,
                s=s.score_id,
                run=run_id,
                rule=d.rule,
                pv=POLICY_VERSION,
                csm=s.csm_id,
                queue=d.queue,
                missing=s.csm_id is None,
                tr=transition_ids.get(d.account_id),
                b=briefing_id,
                p=s.probability,
                reasons=json.dumps([entry]),
            )
            if inserted:
                counts.tasks_created += 1
                counts.briefings += 1
                _event(conn, tid, task_id, run_id, "created", {"rule": d.rule, "queue": d.queue})
        elif d.kind == "append":
            updated = _rows(
                conn,
                "UPDATE tasks SET reasons = reasons || CAST(:entry AS jsonb), updated_at = now()"
                " WHERE task_id = :t AND NOT reasons @> CAST(:probe AS jsonb) RETURNING task_id",
                t=d.task_id,
                entry=json.dumps([entry]),
                probe=json.dumps([{"run_id": str(run_id)}]),
            )
            if updated:
                counts.reasons_appended += 1
                _event(conn, tid, d.task_id, run_id, "reasons_appended", {"rule": d.rule})
        else:  # budget_exceeded: recorded, not tasked
            counts.budget_exceeded += 1
            _event(
                conn,
                tid,
                None,
                run_id,
                "budget_exceeded",
                {"account_id": str(d.account_id), "rule": d.rule, "queue": d.queue},
            )

    stage_ms["policy_and_tasks"] = round((perf_counter() - started) * 1000)
    started = perf_counter()
    counts.tasks_escalated = escalate(conn, tid, run_id, run_date, by_account)
    stage_ms["escalation"] = round((perf_counter() - started) * 1000)
    return counts


def escalate(conn, tid, run_id, run_date: date, by_account: dict) -> int:
    """Open tasks older than 5 business days, on accounts renewing within 60 days."""
    escalated = 0
    for t in _rows(conn, "SELECT task_id, account_id, run_date FROM tasks WHERE status = 'open'"):
        s = by_account.get(t.account_id)
        if s is None or s.days_to_renewal > ESCALATE_WITHIN_RENEWAL_DAYS:
            continue
        age = int(np.busday_count(t.run_date, run_date))
        if age < ESCALATE_AFTER_BUSINESS_DAYS:
            continue
        moved = _rows(
            conn,
            "UPDATE tasks SET status = 'escalated', escalated_at = now(), updated_at = now()"
            " WHERE task_id = :t AND status = 'open' RETURNING task_id",
            t=t.task_id,
        )
        if moved:
            escalated += 1
            _event(
                conn,
                tid,
                t.task_id,
                run_id,
                "escalated",
                {"business_days_open": age, "days_to_renewal": s.days_to_renewal},
            )
    return escalated


def _features(conn, snapshot_ids: list) -> dict:
    if not snapshot_ids:
        return {}
    rows = _rows(
        conn,
        "SELECT snapshot_id, features FROM snapshots WHERE snapshot_id = ANY(:ids)",
        ids=list(set(snapshot_ids)),
    )
    return {r.snapshot_id: r.features for r in rows}


def _event(conn, tid, task_id, run_id, kind: str, detail: dict) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO task_events (tenant_id, task_id, run_id, kind, detail)"
            " VALUES (:tid, :task, :run, :kind, CAST(:detail AS jsonb))"
        ),
        {"tid": tid, "task": task_id, "run": run_id, "kind": kind, "detail": json.dumps(detail)},
    )
