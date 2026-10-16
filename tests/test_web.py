"""Web app tests: list, score and health pages, tenant isolation, auth, and a light import."""

from __future__ import annotations

import base64
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import get_engine, tenant_tx
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


def switch(client, tenant_id):
    r = client.post(
        "/switch", data={"tenant_id": str(tenant_id), "csm_id": str(first_csm(tenant_id))}
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
    assert "Margin check" in r.text and "account_tenure_days" in r.text
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
        "/switch", data={"tenant_id": str(seeded[0]), "csm_id": str(first_csm(seeded[1]))}
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
