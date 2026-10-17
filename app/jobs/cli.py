"""Command line for the jobs group: ``nightly``, ``bootstrap`` and ``narrate``.

    python -m app nightly   --as-of <ts> [--tenant <slug or uuid>]
    python -m app bootstrap --config <yaml> [--data data]
    python -m app narrate   [--tenant <slug or uuid>]

``nightly`` runs the same function the scheduler fires, with ``trigger='cli'``. It is how a
missed night is recovered: ``nightly --as-of <that night>``. Both exit 1 if any tenant failed
or was blocked by the data-quality gate, so a shell or a CI step notices.

``narrate`` narrates the briefings of each tenant's latest succeeded night that have no narration
yet (``app.jobs.narrate``). The nightly job does this by itself when ``LLM_API_KEY`` is set; the
command is for a night that ran before the key was.
"""

from __future__ import annotations

import argparse
import json
import sys


def _exit_code(runs) -> int:
    return 1 if any(run.status in ("failed", "blocked") for run in runs) else 0


def _nightly(args: argparse.Namespace) -> int:
    from app.config import parse_utc
    from app.jobs.nightly import run_nightly

    as_of = parse_utc(args.as_of)
    print(f"nightly at {as_of.isoformat()}")
    runs = run_nightly(as_of, args.tenant, trigger="cli", force=args.force)
    for run in runs:
        line = f"{run.tenant_id}  {run.status:<9} {json.dumps(run.counts)}"
        print(f"{line}  {run.error}" if run.error else line)
    return _exit_code(runs)


def _narrate(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    from app.db import tenant_ids, tenant_tx
    from app.jobs.narrate import configured_client, narrate_run

    if configured_client() is None:
        print("narration is off: set LLM_API_KEY", file=sys.stderr)
        return 1
    for tid in tenant_ids(args.tenant):
        with tenant_tx(tid) as conn:
            run_id = conn.execute(
                sa.text(
                    "SELECT run_id FROM job_runs WHERE job_name = 'nightly'"
                    " AND status = 'succeeded' ORDER BY run_date DESC LIMIT 1"
                )
            ).scalar_one_or_none()
        counts = narrate_run(tid, run_id) if run_id else {}
        print(f"{tid}  {json.dumps(counts)}")
    return 0


def _bootstrap(args: argparse.Namespace) -> int:
    from app.jobs.bootstrap import bootstrap

    return _exit_code(bootstrap(args.config, args.data))  # bootstrap prints its own progress


def main(argv: list[str]) -> int:
    """``argv[0]`` is the command name, as app/__main__.py passes it."""
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", choices=["nightly", "bootstrap", "narrate"])
    parser.add_argument("--as-of", help="ISO 8601 UTC timestamp (nightly)")
    parser.add_argument("--tenant", help="tenant slug or uuid; default is every tenant")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run a succeeded night; refused unless it is the tenant's latest (nightly)",
    )
    parser.add_argument("--config", help="config YAML (bootstrap)")
    parser.add_argument("--data", default="data", help="generated data directory (bootstrap)")
    args = parser.parse_args(argv)
    if args.command == "nightly":
        if not args.as_of:
            parser.error("nightly needs --as-of")
        return _nightly(args)
    if args.command == "narrate":
        return _narrate(args)
    if not args.config:
        parser.error("bootstrap needs --config")
    return _bootstrap(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
