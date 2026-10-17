"""Web app tests: list, score and health pages, tenant isolation, auth, and a light import."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import get_engine, make_engine, tenant_tx
from tests.helpers import ROOT, purge_tenants, seed_tiny

PASSWORD = "test-web-password"


def basic(password):
    """An Authorization header; the username is ignored by the app."""
    token = base64.b64encode(f"u:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("web_data"))
    yield ids
    purge_tenants(database_url, ids)


@pytest.fixture(scope="module")
def client(seeded, monkeypatch_module):
    monkeypatch_module.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch_module.setenv("SESSION_SECRET", "test-secret")
    from app.web.main import create_app

    return TestClient(create_app(), headers=basic(PASSWORD), follow_redirects=False)


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def first_csm(tenant_id):
    """A CSM that owns scored accounts, so the list is not empty."""
    with tenant_tx(tenant_id) as conn:
        return conn.execute(
            sa.text("SELECT csm_id FROM scores WHERE csm_id IS NOT NULL LIMIT 1")
        ).scalar_one()


def csrf(client, tenant_id) -> str:
    """The session's CSRF token, read from the switcher form (task 6.4)."""
    page = client.get(f"/switch?tenant_id={tenant_id}").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def switch(client, tenant_id):
    r = client.post(
        "/switch",
        data={
            "tenant_id": str(tenant_id),
            "csm_id": str(first_csm(tenant_id)),
            "csrf": csrf(client, tenant_id),
        },
    )
    assert r.status_code == 303
    return r


def one_score(tenant_id):
    with tenant_tx(tenant_id) as conn:
        return conn.execute(
            sa.text(
                "SELECT score_id, account_id FROM scores "
                "WHERE csm_id IS NOT NULL ORDER BY rank_in_book LIMIT 1"
            )
        ).one()


def test_requires_basic_auth(seeded, monkeypatch_module):
    from app.web.main import create_app

    monkeypatch_module.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch_module.setenv("SESSION_SECRET", "test-secret")
    bare = TestClient(create_app())
    assert bare.get("/list").status_code == 401
    assert bare.get("/list", headers=basic("wrong")).status_code == 401
    assert bare.get("/health").status_code == 200  # open on purpose


def test_list_renders_ranked_book(client, seeded):
    assert client.get("/list").status_code == 303  # no session yet: go pick one
    page = client.get("/switch")
    assert page.status_code == 200 and seeded[0] is not None
    switch(client, seeded[0])
    r = client.get("/list")
    assert r.status_code == 200
    assert "Monday list" in r.text and "Rank score" in r.text
    with tenant_tx(seeded[0]) as conn:
        expected = conn.execute(
            sa.text(
                "SELECT count(*) FROM scores WHERE csm_id = :c "
                "AND as_of = (SELECT max(as_of) FROM scores)"
            ),
            {"c": first_csm(seeded[0])},
        ).scalar_one()
    assert r.text.count("/scores/") == expected > 0


def test_score_page_shows_margin_and_events(client, seeded):
    switch(client, seeded[0])
    score_id, _ = one_score(seeded[0])
    r = client.get(f"/scores/{score_id}")
    assert r.status_code == 200
    assert "Reconciliation" in r.text and "account_tenure_days" in r.text
    assert "<strong>reconciles</strong>" in r.text  # the page states the check's result
    with tenant_tx(seeded[0]) as conn:
        expected, margin, contributions = conn.execute(
            sa.text("SELECT expected_value, margin, contributions FROM scores WHERE score_id = :s"),
            {"s": score_id},
        ).one()
    assert expected + sum(contributions.values()) == pytest.approx(margin)

    events = client.get(f"/scores/{score_id}/events/total_event_count")
    assert events.status_code == 200 and "Raw events behind total_event_count" in events.text
    from_row = client.get(f"/scores/{score_id}/events/arr")
    assert "not from events" in from_row.text
    assert client.get(f"/scores/{score_id}/events/nope").status_code == 404


def test_account_timeline_renders(client, seeded):
    switch(client, seeded[0])
    _, account_id = one_score(seeded[0])
    r = client.get(f"/accounts/{account_id}")
    assert r.status_code == 200 and "Timeline" in r.text and "Score history" in r.text


def test_other_tenants_ids_return_404(client, seeded):
    switch(client, seeded[0])
    other_score, other_account = one_score(seeded[1])
    assert client.get(f"/scores/{other_score}").status_code == 404
    assert client.get(f"/scores/{other_score}/events/total_event_count").status_code == 404
    assert client.get(f"/accounts/{other_account}").status_code == 404
    # The switcher also refuses a CSM that belongs to a different tenant.
    r = client.post(
        "/switch",
        data={
            "tenant_id": str(seeded[0]),
            "csm_id": str(first_csm(seeded[1])),
            "csrf": csrf(client, seeded[0]),
        },
    )
    assert r.status_code == 404


def test_cookie_is_strict_and_httponly(seeded, monkeypatch_module):
    from app.web.main import create_app

    monkeypatch_module.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch_module.setenv("SESSION_SECRET", "test-secret")
    c = TestClient(create_app(), headers=basic(PASSWORD), follow_redirects=False)
    cookie = switch(c, seeded[0]).headers["set-cookie"].lower()
    assert "samesite=strict" in cookie and "httponly" in cookie


def test_health_reports_counts_and_model(client, seeded):
    r = TestClient(client.app).get("/health")  # no credentials
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["tenants"] >= 2 and body["accounts"] >= 100
    with get_engine().connect() as conn:
        active = conn.execute(
            sa.text("SELECT model_version FROM model_versions WHERE is_active")
        ).scalar()
    assert body["model_version"] == active
    assert "last_job_run" in body


def test_importing_web_loads_no_pandas():
    code = (
        "import sys, app.web.main; "
        "bad = [m for m in ('pandas', 'numpy', 'sklearn') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )
    env = {**os.environ, "BASIC_AUTH_PASSWORD": "x", "SESSION_SECRET": "x"}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env)
    assert result.returncode == 0


def test_account_page_hides_contract_cycles_after_as_of(client, seeded):
    """contracts.csv runs ahead of time; a cycle starting after the latest score must not show."""
    with tenant_tx(seeded[0]) as conn:
        row = conn.execute(
            sa.text(
                "SELECT account_id, cycle_number, start_date, arr FROM contracts "
                "WHERE start_date > (SELECT max(as_of) FROM scores) LIMIT 1"
            )
        ).one()
        past = (
            conn.execute(
                sa.text(
                    "SELECT start_date FROM contracts WHERE account_id = :a "
                    "AND start_date <= (SELECT max(as_of) FROM scores)"
                ),
                {"a": row.account_id},
            )
            .scalars()
            .all()
        )
    switch(client, seeded[0])
    page = client.get(f"/accounts/{row.account_id}")
    assert page.status_code == 200
    assert f'<td class="n">{row.cycle_number}</td>' not in page.text

    assert past and f'<td class="n">{row.cycle_number - 1}</td>' in page.text  # started cycles show


def test_account_page_shows_a_new_cycle_only_once_its_renewal_event_arrived(
    client, seeded, database_url
):
    """A cycle that has started by date but whose renewal event is not in yet must not show."""
    tid = seeded[0]
    with tenant_tx(tid) as conn:
        latest = conn.execute(sa.text("SELECT max(as_of) FROM scores")).scalar_one()
        row = conn.execute(
            sa.text(
                """
                SELECT e.normalised_event_id, e.received_at, c.account_id, c.cycle_number
                FROM contracts c JOIN normalised_events e
                  ON e.account_id = c.account_id
                 AND e.event_type = 'opportunity_stage_changed'
                 AND (e.attrs->>'renewal_date')::date = c.start_date
                WHERE c.cycle_number > 1 AND e.received_at <= :latest
                LIMIT 1
                """
            ),
            {"latest": latest},
        ).one()
    switch(client, tid)
    cell = f'<td class="n">{row.cycle_number}</td>'
    assert cell in client.get(f"/accounts/{row.account_id}").text
    # Move the event's arrival to after the latest score night (as the admin role, since the
    # runtime roles cannot update events), check the cycle is hidden, then put it back.
    admin = make_engine(database_url, poolclass=sa.pool.NullPool)
    move = sa.text("UPDATE normalised_events SET received_at = :r WHERE normalised_event_id = :e")
    try:
        with admin.begin() as conn:
            conn.execute(move, {"r": latest + timedelta(hours=1), "e": row.normalised_event_id})
        assert cell not in client.get(f"/accounts/{row.account_id}").text
    finally:
        with admin.begin() as conn:
            conn.execute(move, {"r": row.received_at, "e": row.normalised_event_id})
        admin.dispose()


def test_unassigned_book_is_reachable(client, seeded, database_url):
    """Accounts with no CSM are shown under the virtual 'Unassigned' owner."""
    tid = seeded[0]
    assert "unassigned" in client.get(f"/switch?tenant_id={tid}").text
    # The runtime role cannot UPDATE scores, so the test edits one row as the admin user.
    admin = make_engine(database_url, poolclass=sa.pool.NullPool)
    with admin.begin() as conn:
        sid = conn.execute(
            sa.text(
                "UPDATE scores SET csm_id = NULL WHERE score_id = ("
                "SELECT score_id FROM scores WHERE tenant_id = :t "
                "AND as_of = (SELECT max(as_of) FROM scores WHERE tenant_id = :t) LIMIT 1) "
                "RETURNING score_id"
            ),
            {"t": tid},
        ).scalar_one()
    try:
        r = client.post(
            "/switch",
            data={"tenant_id": str(tid), "csm_id": "unassigned", "csrf": csrf(client, tid)},
        )
        assert r.status_code == 303
        page = client.get("/list")
        assert page.status_code == 200 and f"/scores/{sid}" in page.text
        assert page.text.count("/scores/") == 1
    finally:
        with admin.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE scores s SET csm_id = a.csm_id FROM accounts a "
                    "WHERE s.score_id = :s AND a.account_id = s.account_id"
                ),
                {"s": sid},
            )
        admin.dispose()


def test_last_job_run_orders_by_night_not_started_at(seeded, database_url):
    """A recovery run for an older night starts later but is not the newest run."""
    from app.web import queries as q

    tid = seeded[0]
    admin = make_engine(database_url, poolclass=sa.pool.NullPool)
    insert = sa.text(
        "INSERT INTO job_runs (run_id, job_name, tenant_id, run_date, as_of, trigger, status, "
        "started_at) VALUES (gen_random_uuid(), 'nightly', :t, :d, :a, 'cli', 'succeeded', :s)"
    )
    nights = [  # (run_date, started_at): the older night started last
        ("2031-03-10", "2031-03-10T02:00:00Z"),
        ("2031-03-03", "2031-03-11T09:00:00Z"),
    ]
    try:
        with admin.begin() as conn:
            for d, started in nights:
                conn.execute(insert, {"t": tid, "d": d, "a": d + "T00:00:00Z", "s": started})
        with tenant_tx(tid) as conn:
            got = q.last_job_run(conn)
        assert str(got["run_date"]) == "2031-03-10"
    finally:
        with admin.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM job_runs WHERE tenant_id = :t AND run_date >= '2031-01-01'"),
                {"t": tid},
            )
        admin.dispose()
