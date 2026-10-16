"""Worker and clock, checked without a database and without starting the scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app import clock
from app.config import load_config
from app.jobs import worker


def test_scheduler_is_utc_cron_with_one_instance():
    scheduler = worker.build_scheduler("*/2 * * * *")
    assert isinstance(scheduler, BlockingScheduler) and not scheduler.running
    assert scheduler.timezone == UTC
    (job,) = scheduler.get_jobs()
    assert job.func is worker.nightly_tick
    assert job.max_instances == 1 and job.coalesce is True
    assert isinstance(job.trigger, CronTrigger) and job.trigger.timezone == UTC
    after = datetime(2026, 10, 16, 2, 1, 30, tzinfo=UTC)
    assert job.trigger.get_next_fire_time(None, after) == datetime(2026, 10, 16, 2, 2, tzinfo=UTC)


def test_default_cron_fires_at_two_utc_whatever_the_local_zone():
    trigger = worker.build_scheduler(worker.DEFAULT_CRON).get_jobs()[0].trigger
    india = timezone(timedelta(hours=5, minutes=30))
    after = datetime(2026, 10, 16, 9, 0, tzinfo=india)  # 03:30 UTC, past today's fire
    assert trigger.get_next_fire_time(None, after) == datetime(2026, 10, 17, 2, 0, tzinfo=UTC)


def test_bad_cron_fails_at_start():
    with pytest.raises(ValueError):
        worker.build_scheduler("every night at two")


def test_tick_uses_the_clock_override_truncated_to_the_minute(monkeypatch):
    calls = []
    monkeypatch.setenv("CLOCK_OVERRIDE", "2026-10-13T02:00:37.5Z")
    monkeypatch.setattr(
        worker, "run_nightly", lambda as_of, trigger: calls.append((as_of, trigger)) or []
    )
    worker.nightly_tick()
    assert calls == [(datetime(2026, 10, 13, 2, 0, tzinfo=UTC), "schedule")]


def test_clock_reads_the_override_or_the_wall_clock(monkeypatch):
    monkeypatch.setenv("CLOCK_OVERRIDE", "2026-10-13T07:30:00+05:30")
    assert clock.now_utc() == datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
    monkeypatch.setenv("CLOCK_OVERRIDE", "")
    assert clock.now_utc().tzinfo == UTC


class FakeScheduler:
    def __init__(self, events):
        self.events = events

    def start(self):
        self.events.append("start")


@pytest.mark.parametrize("model_exists", [False, True])
def test_main_migrates_bootstraps_only_when_empty_then_starts(monkeypatch, model_exists):
    events = []
    monkeypatch.setenv("BOOTSTRAP_CONFIG", "config/small.yaml")
    monkeypatch.setenv("DATA_DIR", "/srv/data")
    monkeypatch.setenv("NIGHTLY_CRON", "*/2 * * * *")
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "migrate", lambda: events.append("migrate"))
    monkeypatch.setattr(worker, "has_model", lambda: model_exists)
    monkeypatch.setattr(
        worker, "bootstrap", lambda cfg, data: events.append(("bootstrap", cfg, data))
    )
    monkeypatch.setattr(
        worker, "run_nightly", lambda as_of, trigger: events.append(("night", as_of, trigger)) or []
    )
    monkeypatch.setattr(
        worker,
        "build_scheduler",
        lambda cron: events.append(("cron", cron)) or FakeScheduler(events),
    )
    assert worker.main(["worker"]) == 0
    bootstrapped = [] if model_exists else [("bootstrap", "config/small.yaml", "/srv/data")]
    # The bootstrap night runs on every start, so a worker killed after train but before that
    # night finished completes it on restart even though a model already exists.
    night = ("night", load_config("config/small.yaml").train_as_of, "bootstrap")
    assert events == ["migrate", *bootstrapped, night, ("cron", "*/2 * * * *"), "start"]
