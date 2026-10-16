"""``python -m app worker``: the long-running process that fires the nightly job on a schedule.

On start it:

1. runs ``migrate`` (idempotent; the worker is the only process that migrates);
2. runs ``bootstrap($BOOTSTRAP_CONFIG, $DATA_DIR)`` if ``model_versions`` is empty, i.e. on the
   first start against a fresh database;
3. starts an APScheduler 3.x ``BlockingScheduler`` in UTC with one job: a ``CronTrigger`` built
   from ``$NIGHTLY_CRON`` (five fields, UTC, default 02:00), ``max_instances=1`` so two nights
   never overlap, ``coalesce=True`` so a backlog of missed fires runs once, not many times.

Each fire calls ``run_nightly(as_of=now_utc() truncated to the minute, trigger="schedule")``.
``now_utc`` honours ``CLOCK_OVERRIDE``. The scheduler lives here and never inside the web server,
so there is exactly one schedule however many web workers run.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections import Counter
from datetime import UTC

import sqlalchemy as sa
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.clock import now_utc
from app.db import get_engine
from app.jobs.bootstrap import bootstrap
from app.jobs.nightly import run_nightly
from app.migrate import migrate

log = logging.getLogger(__name__)

DEFAULT_CRON = "0 2 * * *"
DEFAULT_BOOTSTRAP_CONFIG = "config/full.yaml"
DEFAULT_DATA_DIR = "data"
# A fire that starts late (a busy host, a paused VM) still runs if it is less than an hour late.
# APScheduler's default grace is one second, which would silently skip such a night.
MISFIRE_GRACE_SECONDS = 3600


def nightly_tick() -> None:
    """The scheduled job. ``as_of`` is the fire minute, so a fire at 02:00:00.4 reads 02:00."""
    as_of = now_utc().replace(second=0, microsecond=0)
    runs = run_nightly(as_of, trigger="schedule")
    summary = dict(Counter(run.status for run in runs))
    log.info("nightly at %s finished: %s", as_of.isoformat(), summary)


def build_scheduler(cron: str) -> BlockingScheduler:
    """A UTC scheduler holding the one nightly job. Not started, so tests can inspect it.

    ``CronTrigger.from_crontab`` raises ``ValueError`` on a bad expression, so a typo in
    ``NIGHTLY_CRON`` stops the worker at start, not silently at 2 a.m.
    """
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        nightly_tick,
        CronTrigger.from_crontab(cron, timezone=UTC),
        id="nightly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    return scheduler


def has_model() -> bool:
    """True once any model has been trained. ``model_versions`` is global, with no RLS."""
    with get_engine("app").connect() as conn:
        return bool(conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM model_versions)")).scalar())


def stop(signum, frame) -> None:
    """Turn SIGTERM (``docker stop``, a redeploy) into a normal exit.

    Python running as PID 1 in a container ignores SIGTERM unless a handler is installed, so
    without this every stop would wait for the SIGKILL timeout.
    """
    raise SystemExit(0)


def main(argv: list[str]) -> int:
    """Entry point for ``python -m app worker``. Takes no arguments; configured by env."""
    logging.Formatter.converter = time.gmtime  # log timestamps in UTC, like everything else
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s"
    )
    signal.signal(signal.SIGTERM, stop)

    migrate()
    if not has_model():
        log.info("model_versions is empty: running bootstrap first")
        bootstrap(
            os.environ.get("BOOTSTRAP_CONFIG") or DEFAULT_BOOTSTRAP_CONFIG,
            os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR,
        )

    cron = os.environ.get("NIGHTLY_CRON") or DEFAULT_CRON
    scheduler = build_scheduler(cron)
    log.info("worker scheduling nightly with cron %r (UTC)", cron)
    scheduler.start()  # blocks until SIGTERM or Ctrl+C
    return 0
