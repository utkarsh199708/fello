"""Step 6: every page renders for a seeded tenant, the task buttons work behind CSRF, the list
sorts, marks and buckets, and another tenant's ids answer 404.

The tiny tenants are seeded, then two nights run so tasks, transitions and briefings exist.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import tenant_tx
from app.jobs.nightly import run_id_for, run_nightly
from app.web.main import business_days
from app.web.queries import marker
from tests.helpers import purge_tenants, seed_tiny

PASSWORD = "test-dashboard-password"
NIGHT1 = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
NIGHT2 = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def seeded(database_url, tmp_path_factory):
    ids = seed_tiny(tmp_path_factory.mktemp("tiny-dashboard"))
    run_nightly(NIGHT1)
    run_nightly(NIGHT2)
    yield ids
    purge_tenants(database_url, ids)


@pytest.fixture(scope="module")
def mp():
    with pytest.MonkeyPatch.context() as m:
        yield m


@pytest.fixture(scope="module")
def client(seeded, mp):
    mp.setenv("BASIC_AUTH_PASSWORD", PASSWORD)
    mp.setenv("SESSION_SECRET", "test-secret")
    mp.setenv("CLOCK_OVERRIDE", "2026-10-14T09:00:00Z")  # the morning after NIGHT2
    from app.web.main import create_app

    token = base64.b64encode(f"u:{PASSWORD}".encode()).decode()
    return TestClient(
        create_app(), headers={"Authorization": f"Basic {token}"}, follow_redirects=False
    )


def one(tid, sql, **params):
    with tenant_tx(tid) as conn:
        return conn.execute(sa.text(sql), params).first()


def csrf(client, tid) -> str:
    page = client.get(f"/switch?tenant_id={tid}").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def login(client, tid, csm_id):
    r = client.post(
        "/switch", data={"tenant_id": str(tid), "csm_id": str(csm_id), "csrf": csrf(client, tid)}
    )
    assert r.status_code == 303


def task_csm(tid):
    """A CSM who has an open task, so the list has buttons on it."""
    return one(
        tid, "SELECT csm_id FROM tasks WHERE csm_id IS NOT NULL AND status = 'open' LIMIT 1"
    ).csm_id


def test_every_page_renders(client, seeded):
    tid = seeded[0]
    login(client, tid, task_csm(tid))
    account = one(tid, "SELECT account_id FROM tasks LIMIT 1").account_id
    score = one(tid, "SELECT score_id FROM scores WHERE run_id IS NOT NULL LIMIT 1").score_id
    run = run_id_for(tid, NIGHT2.date())
    pages = [
        "/list",
        "/list?sort=probability",
        "/list?sort=arr",
        "/list?sort=days_to_renewal",
        "/list?sort=nonsense",
        "/runs",
        f"/runs/{run}",
        "/quarantine",
        "/misses",
        f"/accounts/{account}",
        f"/scores/{score}",
        f"/switch?tenant_id={tid}",
    ]
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
    listing = client.get("/list").text
    assert 'id="staleness"' in listing and "Not enough signal" in listing
    assert "Done</button>" in listing and 'name="csrf"' in listing
    assert "Next scheduled run" in client.get("/runs").text
    assert "Tasks raised this night" in client.get(f"/runs/{run}").text
    assert "Tasks" in client.get(f"/accounts/{account}").text


def test_the_list_is_stale_when_no_night_succeeded_for_a_day(client, seeded, mp):
    login(client, seeded[0], task_csm(seeded[0]))
    assert 'class="fresh" id="staleness"' in client.get("/list").text
    # finished_at is stamped by the wall clock when the test nights run, so "over a day later"
    # must be measured from the wall clock, not from a fixed date.
    mp.setenv("CLOCK_OVERRIDE", (datetime.now(UTC) + timedelta(days=2)).isoformat())
    try:
        assert 'class="stale" id="staleness"' in client.get("/list").text
    finally:
        mp.setenv("CLOCK_OVERRIDE", "2026-10-14T09:00:00Z")


def test_an_escalated_task_is_pinned_to_the_top(client, seeded):
    tid = seeded[0]
    csm = task_csm(tid)
    with tenant_tx(tid) as conn:
        last = conn.execute(
            sa.text(
                "SELECT t.task_id, a.name FROM tasks t JOIN accounts a USING (account_id)"
                " JOIN scores s ON s.account_id = t.account_id AND s.as_of = :d"
                " WHERE t.csm_id = :c AND t.status = 'open' ORDER BY s.rank_score ASC LIMIT 1"
            ),
            {"c": csm, "d": NIGHT2},
        ).one()
        conn.execute(
            sa.text("UPDATE tasks SET status = 'escalated' WHERE task_id = :t"),
            {"t": last.task_id},
        )
    login(client, tid, csm)
    page = client.get("/list?sort=arr").text
    first = page.index("<tbody>")
    assert last.name in page[first : page.index("</tr>", first)]
    assert 'class="badge escalated"' in page


def test_task_buttons_need_csrf_and_write_an_audit_row(client, seeded):
    tid = seeded[0]
    login(client, tid, task_csm(tid))
    task = one(tid, "SELECT task_id FROM tasks WHERE status = 'open' LIMIT 1").task_id
    url = f"/tasks/{task}/status"
    assert client.post(url, data={"status": "done"}).status_code == 403  # no token
    assert client.post(url, data={"status": "done", "csrf": "forged"}).status_code == 403
    token = csrf(client, tid)
    assert client.post(url, data={"status": "dismissed", "csrf": token}).status_code == 400
    r = client.post(url, data={"status": "dismissed", "reason": "renewal signed", "csrf": token})
    assert r.status_code == 303
    row = one(
        tid,
        "SELECT status, dismissed_reason, dismissed_probability FROM tasks WHERE task_id = :t",
        t=task,
    )
    assert row.status == "dismissed" and row.dismissed_reason == "renewal signed"
    assert row.dismissed_probability is not None
    event = one(
        tid,
        "SELECT detail FROM task_events WHERE task_id = :t AND kind = 'status_changed'",
        t=task,
    )
    assert event.detail["to"] == "dismissed" and event.detail["from"] == "open"
    # A closed task cannot change again.
    assert client.post(url, data={"status": "done", "csrf": token}).status_code == 404


def test_snooze_sets_a_date_a_week_out(client, seeded):
    tid = seeded[0]
    login(client, tid, task_csm(tid))
    task = one(tid, "SELECT task_id FROM tasks WHERE status = 'open' LIMIT 1").task_id
    r = client.post(f"/tasks/{task}/status", data={"status": "snoozed", "csrf": csrf(client, tid)})
    assert r.status_code == 303
    row = one(tid, "SELECT status, snoozed_until FROM tasks WHERE task_id = :t", t=task)
    assert row.status == "snoozed" and row.snoozed_until is not None


def test_another_tenants_ids_answer_404(client, seeded):
    mine, other = seeded
    login(client, mine, task_csm(mine))
    run = run_id_for(other, NIGHT2.date())
    task = one(other, "SELECT task_id FROM tasks LIMIT 1").task_id
    account = one(other, "SELECT account_id FROM accounts LIMIT 1").account_id
    score = one(other, "SELECT score_id FROM scores LIMIT 1").score_id
    for path in (f"/runs/{run}", f"/accounts/{account}", f"/scores/{score}"):
        assert client.get(path).status_code == 404, path
    r = client.post(f"/tasks/{task}/status", data={"status": "done", "csrf": csrf(client, mine)})
    assert r.status_code == 404
    assert one(other, "SELECT status FROM tasks WHERE task_id = :t", t=task).status != "done"


def test_the_switcher_refuses_a_post_without_csrf(client, seeded):
    tid = seeded[0]
    r = client.post("/switch", data={"tenant_id": str(tid), "csm_id": "unassigned"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("rank", "monday", "expected"),
    [(3, 12, "new"), (3, None, "new"), (5, 7, "moved up"), (6, 4, "moved down"), (4, 4, "")],
)
def test_since_monday_markers(rank, monday, expected):
    row = {"rank_in_book": rank, "monday_rank": monday, "k": 8, "monday_as_of": date(2026, 10, 12)}
    assert marker(row) == expected


def test_business_days_matches_the_escalation_rule():
    assert business_days(date(2026, 10, 12), date(2026, 10, 19)) == 5  # Monday to Monday
    assert business_days(date(2026, 10, 16), date(2026, 10, 19)) == 1  # Friday to Monday
    assert business_days(date(2026, 10, 19), date(2026, 10, 12)) == 0
