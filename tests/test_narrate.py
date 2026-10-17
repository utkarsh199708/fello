"""The optional LLM narration (``app.jobs.narrate``).

It runs after a night, sends only the reason list, keeps only text whose every number is in its
input, narrates a briefing once, and never fails or changes a night. A fake client stands in for
the API, so nothing here touches a network.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import tenant_tx
from app.jobs.narrate import check, narrate_run, numbers_in
from app.jobs.nightly import run_id_for, run_nightly
from tests.helpers import purge_tenants, seed_tiny

NIGHT1 = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
PASSWORD = "test-narrate-password"
FACTS = {
    "renewal_risk": "42%",
    "arr": "12,000",
    "days_to_renewal": "40",
    "reasons": [
        {"reason": "support tickets in the last 30 days", "value": "7"},
        {"reason": "change in active users against the prior 3 months", "value": "-35%"},
    ],
}


def test_numbers_are_read_without_commas_signs_or_percent_signs():
    assert numbers_in("Risk 42% on $12,000; usage -35% and 7 tickets.") == [42, 12000, 35, 7]


def test_a_note_that_only_uses_the_input_passes():
    note = (
        "Renewal risk is 42% on 12,000 ARR, with 7 support tickets in the last 30 days and active"
        " users down 35%. Start with the open tickets."
    )
    assert check(note, FACTS) is None


def test_a_note_with_an_invented_number_is_rejected():
    problem = check("Renewal risk is 42% and 5 champions left last quarter.", FACTS)
    assert problem is not None and "5" in problem


def test_an_empty_or_long_note_is_rejected():
    assert check("  ", FACTS) == "empty"
    assert check("x" * 601, FACTS).startswith("longer than")


def test_without_a_key_nothing_is_called(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    assert narrate_run(uuid.uuid4(), uuid.uuid4()) == {}


# --- on the tiny tenants ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny-narrate"))
    run_nightly(NIGHT1)  # the first night under the policy: tasks and briefings
    yield ids
    purge_tenants(database_url, ids)


def scalar(tid, sql, **params):
    with tenant_tx(tid) as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


def grounded(facts):
    return f"Renewal risk is {facts['renewal_risk']}. Start with the reasons listed below."


def test_the_night_itself_narrates_nothing_without_a_key(seeded):
    for tid in seeded:
        assert scalar(tid, "SELECT count(*) FROM briefings") > 0
        assert scalar(tid, "SELECT count(*) FROM narrations") == 0


def test_each_briefing_is_narrated_once_within_the_cap(seeded):
    tid = seeded[0]
    run = run_id_for(tid, NIGHT1.date())
    briefings = scalar(tid, "SELECT count(*) FROM briefings WHERE run_id = :r", r=run)
    calls = []

    def client(facts):
        calls.append(facts)
        return grounded(facts)

    assert narrate_run(tid, run, client=client, model="fake", limit=1) == {
        "ok": 1,
        "rejected": 0,
        "error": 0,
    }
    rest = narrate_run(tid, run, client=client, model="fake")
    assert rest["ok"] == briefings - 1 and len(calls) == briefings
    nothing = {"ok": 0, "rejected": 0, "error": 0}
    assert narrate_run(tid, run, client=client, model="fake") == nothing
    assert len(calls) == briefings  # nothing was sent twice

    # Only the reason list and three numbers left the system: no account name, no email.
    sent = json.dumps(calls)
    with tenant_tx(tid) as conn:
        names = conn.execute(sa.text("SELECT name FROM accounts")).scalars().all()
    assert not any(name in sent for name in names) and "@" not in sent
    assert {key for facts in calls for key in facts} == {
        "renewal_risk",
        "arr",
        "days_to_renewal",
        "reasons",
    }
    stored = scalar(tid, "SELECT count(*) FROM narrations WHERE run_id = :r", r=run)
    assert stored == briefings


def test_failures_and_ungrounded_text_are_stored_but_never_shown(seeded):
    tid = seeded[1]
    run = run_id_for(tid, NIGHT1.date())
    briefings = scalar(tid, "SELECT count(*) FROM briefings WHERE run_id = :r", r=run)
    assert briefings >= 2
    calls = {"n": 0}

    def client(facts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("the API was slow")
        return "Renewal risk is 99999%."  # a number that is not in the input

    counts = narrate_run(tid, run, client=client, model="fake")
    assert counts == {"ok": 0, "rejected": briefings - 1, "error": 1}
    assert scalar(tid, "SELECT count(*) FROM narrations WHERE text IS NOT NULL") == 0
    rejected = scalar(tid, "SELECT count(*) FROM narrations WHERE detail LIKE '%99999%'")
    assert rejected == briefings - 1
    # The night's own record is untouched by anything that happened here.
    assert scalar(tid, "SELECT status FROM job_runs WHERE run_id = :r", r=run) == "succeeded"


def test_the_list_shows_a_checked_narration_beside_the_briefing(seeded, monkeypatch):
    tid = seeded[0]  # narrated by the test above
    csm = scalar(
        tid,
        "SELECT t.csm_id FROM tasks t JOIN narrations n USING (briefing_id)"
        " WHERE t.status = 'open' AND t.csm_id IS NOT NULL AND n.status = 'ok' LIMIT 1",
    )
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("CLOCK_OVERRIDE", "2026-10-13T09:00:00Z")
    from app.web.main import create_app

    token = base64.b64encode(f"u:{PASSWORD}".encode()).decode()
    client = TestClient(
        create_app(), headers={"Authorization": f"Basic {token}"}, follow_redirects=False
    )
    page = client.get(f"/switch?tenant_id={tid}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/switch", data={"tenant_id": str(tid), "csm_id": str(csm), "csrf": csrf})
    assert r.status_code == 303
    listing = client.get("/list").text
    assert "Renewal risk is" in listing and "every number checked against them" in listing
