"""Step 5: the agentic nightly job. Policy rules (pure), then the job on the tiny tenants.

The headline properties of PLAN.md section 9:

- identical scores make zero new tasks;
- an oscillating score raises at most one task per three nights;
- a crash part-way and a re-run leave one succeeded row per tenant and no duplicate task;
- escalation fires once;
- a new model rebaselines and seeds.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import get_engine, tenant_tx
from app.jobs import actions as actions_module
from app.jobs.actions import escalate, render_briefing
from app.jobs.nightly import run_id_for, run_nightly
from app.jobs.policy import Candidate, History, decide
from tests.helpers import purge_tenants, seed_tiny

K = 8
CSM = uuid.UUID("00000000-0000-4000-8000-00000000c5a1")


def acct(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def cand(n, rank, p=0.3, csm=CSM, k=K):
    rank_score = (100 - (rank or 99)) * 1.0
    return Candidate(acct(n), csm, uuid.uuid4(), rank, p, rank_score, k)


# --- policy, pure ---------------------------------------------------------------------------


def test_seed_tasks_every_top_k_account_without_an_active_task():
    cands = [cand(n, n) for n in range(1, 13)]
    history = {acct(3): History(active_task_id=uuid.uuid4())}
    plan = decide(cands, history, seed=True)
    created = {d.account_id for d in plan.of_kind("create")}
    assert created == {acct(n) for n in range(1, 9)} - {acct(3)}
    assert {d.rule for d in plan.of_kind("create")} == {"seed"}
    assert [d.account_id for d in plan.of_kind("append")] == [acct(3)]


def test_identical_scores_make_zero_new_tasks():
    """Night 2 with the same ranks: every top-k account was in the top k before. No new task."""
    cands = [cand(n, n) for n in range(1, 21)]
    history = {
        acct(n): History(top_k_previous=(n <= K,), last_task_probability=0.3 if n <= K else None)
        for n in range(1, 21)
    }
    assert decide(cands, history).of_kind("create") == []


def test_an_oscillating_score_raises_at_most_one_task_per_three_nights():
    ranks = [9, 8, 9, 8, 9, 8, 9, 8, 9, 8, 9, 8]  # flips across the k = 8 boundary every night
    previous: list[bool] = []
    created = 0
    for night, rank in enumerate(ranks):
        h = History(top_k_previous=tuple(reversed(previous)), last_task_probability=0.3)
        plan = decide([cand(1, rank)], {acct(1): h})
        created += len(plan.of_kind("create"))  # a CSM closes each task the same day
        previous.append(rank <= K)
        assert created <= night // 3 + 1
    assert created == 1


def test_a_rise_in_probability_raises_a_task_even_outside_the_top_k():
    h = History(top_k_previous=(False, False, False), last_task_probability=0.20)
    assert decide([cand(1, 15, p=0.30)], {acct(1): h}).of_kind("create") == []
    plan = decide([cand(1, 15, p=0.36)], {acct(1): h})
    assert [d.rule for d in plan.of_kind("create")] == ["probability_rise"]


def test_a_dismissal_suppresses_until_the_probability_rises_well_past_it():
    h = History(dismissed_probability=0.40, last_task_probability=0.40)
    assert decide([cand(1, 1, p=0.50)], {acct(1): h}).of_kind("create") == []
    assert len(decide([cand(1, 1, p=0.60)], {acct(1): h}).of_kind("create")) == 1


def test_the_budget_caps_new_tasks_per_queue_and_keeps_the_most_at_risk():
    h = History(top_k_previous=(False,) * 3, last_task_probability=0.10)
    cands = [cand(n, 8 + n, p=0.5) for n in range(1, 11)]  # ten rises outside the top k
    plan = decide(cands, {c.account_id: h for c in cands})
    created = [d.account_id for d in plan.of_kind("create")]
    assert len(created) == K and len(plan.of_kind("budget_exceeded")) == 2
    assert created == [acct(n) for n in range(1, 9)]  # highest rank_score first


def test_ownerless_accounts_go_to_the_cs_lead_queue_and_thin_ones_are_never_tasked():
    plan = decide([cand(1, 1, csm=None), cand(2, None)], {}, seed=True)
    assert [(d.account_id, d.queue) for d in plan.of_kind("create")] == [(acct(1), "cs_lead")]


def test_a_briefing_names_an_unconnected_source_instead_of_a_zero():
    score = SimpleNamespace(probability=0.42, arr=12000, days_to_renewal=40)
    reasons = [
        {"label": "support tickets in the last 30 days", "value": None, "source": "zendesk",
         "connected": False},
        {"label": "active users last month", "value": 3.0, "source": "product",
         "connected": True},
    ]  # fmt: skip
    text = render_briefing(score, reasons)
    assert "Zendesk not connected" in text and "0 tickets" not in text
    assert text.startswith("Renewal risk 42% on 12,000 ARR, renewal in 40 days.")


# --- the job on the tiny tenants ------------------------------------------------------------

NIGHT1 = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
NIGHT2 = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
NIGHT3 = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny-agentic"))
    yield ids
    purge_tenants(database_url, ids)


def count(tid, sql, **params):
    with tenant_tx(tid) as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


def test_the_first_night_seeds_tasks_with_transitions_and_briefings(seeded):
    runs = run_nightly(NIGHT1)
    assert [r.status for r in runs] == ["succeeded", "succeeded"]
    for r in runs:
        actions = r.counts["actions"]
        assert actions["seeded"] and actions["tasks_created"] > 0
        assert actions["transitions"] == r.counts["accounts_scored"]
        tid = r.tenant_id
        assert (
            count(tid, "SELECT count(*) FROM tasks WHERE rule_fired = 'seed'")
            == actions["tasks_created"]
        )
        assert count(tid, "SELECT count(*) FROM briefings") == actions["tasks_created"]
        # At most k new tasks per queue, and every task traces to its score, transition, briefing.
        assert (
            count(
                tid,
                "SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM tasks"
                " GROUP BY csm_id) q",
            )
            <= K
        )
        assert (
            count(
                tid,
                "SELECT count(*) FROM tasks WHERE transition_id IS NULL OR briefing_id IS NULL",
            )
            == 0
        )


def test_a_rerun_writes_nothing_and_force_only_reruns_the_latest_night(seeded):
    before = {tid: count(tid, "SELECT count(*) FROM tasks") for tid in seeded}
    assert [r.status for r in run_nightly(NIGHT1)] == ["no_op", "no_op"]
    assert [r.status for r in run_nightly(NIGHT1, force=True)] == ["succeeded", "succeeded"]
    assert {tid: count(tid, "SELECT count(*) FROM tasks") for tid in seeded} == before


def test_the_second_night_appends_instead_of_duplicating(seeded):
    runs = run_nightly(NIGHT2)
    assert [r.status for r in runs] == ["succeeded", "succeeded"]
    for r in runs:
        assert not r.counts["actions"]["seeded"]
        # One active task per account at most, however often it stays in the top k.
        assert (
            count(
                r.tenant_id,
                "SELECT count(*) FROM (SELECT account_id FROM tasks"
                " WHERE status IN ('open', 'snoozed', 'escalated')"
                " GROUP BY account_id HAVING count(*) > 1) d",
            )
            == 0
        )
    # The earlier night is now not the latest: --force is refused for it.
    assert [r.status for r in run_nightly(NIGHT1, force=True)] == ["no_op", "no_op"]


def test_a_crash_part_way_then_a_rerun_leaves_one_clean_night(seeded, monkeypatch):
    real = actions_module.apply_actions
    calls = {"n": 0}

    def flaky(conn, tid, *args, **kwargs):
        calls["n"] += 1
        if tid == seeded[1] and calls["n"] == 2:
            raise RuntimeError("crash in the actions stage")
        return real(conn, tid, *args, **kwargs)

    monkeypatch.setattr("app.jobs.nightly.apply_actions", flaky)
    assert [r.status for r in run_nightly(NIGHT3)] == ["succeeded", "failed"]
    # The failed tenant's transaction rolled back: no transition or task of that night survived.
    run_id = run_id_for(seeded[1], NIGHT3.date())
    assert count(seeded[1], "SELECT count(*) FROM transitions WHERE run_id = :r", r=run_id) == 0
    monkeypatch.setattr("app.jobs.nightly.apply_actions", real)
    assert [r.status for r in run_nightly(NIGHT3)] == ["no_op", "succeeded"]
    for tid in seeded:
        assert (
            count(
                tid,
                "SELECT count(*) FROM job_runs WHERE run_date = :d AND status = 'succeeded'",
                d=NIGHT3.date(),
            )
            == 1
        )
        assert (
            count(
                tid,
                "SELECT count(*) FROM (SELECT 1 FROM tasks GROUP BY account_id, run_date"
                " HAVING count(*) > 1) d",
            )
            == 0
        )


def test_escalation_fires_once(seeded):
    tid = seeded[0]
    run_id = run_id_for(tid, NIGHT3.date())
    with tenant_tx(tid) as conn:
        open_tasks = (
            conn.execute(sa.text("SELECT account_id FROM tasks WHERE status = 'open'"))
            .scalars()
            .all()
        )
        assert open_tasks
        due = {a: SimpleNamespace(days_to_renewal=30) for a in open_tasks}
        later = date(2026, 10, 30)  # well over five business days after the seed night
        assert escalate(conn, tid, run_id, later, due) == len(open_tasks)
        assert escalate(conn, tid, run_id, later, due) == 0  # idempotent
        events = conn.execute(
            sa.text("SELECT count(*) FROM task_events WHERE kind = 'escalated'")
        ).scalar_one()
        conn.rollback()  # keep the module's tasks as the job left them
    assert events == len(open_tasks)


def test_a_new_model_rebaselines_and_seeds(seeded):
    tid = seeded[0]
    with get_engine("app_batch").begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO model_versions (model_version, kind, feature_set_version, features,"
                " artifact, train_as_of, label_cutoff, anchor_first, anchor_last,"
                " tenant_row_counts, role) SELECT 'qa-older-model', kind, feature_set_version,"
                " features, artifact, train_as_of, label_cutoff, anchor_first, anchor_last,"
                " tenant_row_counts, 'live' FROM model_versions WHERE is_active"
                " ON CONFLICT DO NOTHING"
            )
        )
    # Pretend the last night was scored by another model.
    with tenant_tx(tid) as conn:
        conn.execute(
            sa.text("UPDATE job_runs SET model_version = 'qa-older-model' WHERE run_date = :d"),
            {"d": NIGHT3.date()},
        )
    night4 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    (run,) = run_nightly(night4, tenant=tid)
    assert run.status == "succeeded"
    assert run.counts["actions"]["rebaselined"] and run.counts["actions"]["seeded"]
    assert (
        count(
            tid,
            "SELECT count(*) FROM task_events WHERE kind = 'rebaselined' AND run_id = :r",
            r=run.run_id,
        )
        == 1
    )


# --- the admin trigger (5.7) ----------------------------------------------------------------

PASSWORD = "test-agentic-password"


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from app.web.main import create_app

    token = base64.b64encode(f"u:{PASSWORD}".encode()).decode()
    return TestClient(create_app(), headers={"Authorization": f"Basic {token}"})


def test_the_admin_trigger_runs_a_manual_night(seeded, client):
    night5 = "2026-10-17T02:00:00Z"
    reply = client.post("/admin/jobs/nightly", json={"as_of": night5, "tenant": str(seeded[1])})
    assert reply.status_code == 200, reply.text
    (run,) = reply.json()["runs"]
    assert run["status"] == "succeeded"
    trigger = count(
        seeded[1], "SELECT trigger FROM job_runs WHERE run_id = :r", r=uuid.UUID(run["run_id"])
    )
    assert trigger == "manual"


def test_the_admin_trigger_refuses_forms_unknown_tenants_and_strangers(seeded, client):
    assert client.post("/admin/jobs/nightly", data={"tenant": "x"}).status_code == 415
    assert client.post("/admin/jobs/nightly", json={"tenant": "no-such"}).status_code == 404
    stranger = TestClient(client.app)
    assert stranger.post("/admin/jobs/nightly", json={}).status_code == 401
