"""Command line for the jobs group: ``nightly`` and ``bootstrap``.

    python -m app nightly   --as-of <ts> [--tenant <slug or uuid>]
    python -m app bootstrap --config <yaml> [--data data]

``nightly`` runs the same function the scheduler fires, with ``trigger='cli'``. It is how a
missed night is recovered: ``nightly --as-of <that night>``. Both exit 1 if any tenant failed
or was blocked by the data-quality gate, so a shell or a CI step notices.
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


def _bootstrap(args: argparse.Namespace) -> int:
    from app.jobs.bootstrap import bootstrap

    return _exit_code(bootstrap(args.config, args.data))  # bootstrap prints its own progress


def main(argv: list[str]) -> int:
    """``argv[0]`` is the command name (``nightly`` or ``bootstrap``), as app/__main__.py passes."""
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", choices=["nightly", "bootstrap"])
    parser.add_argument("--as-of", help="ISO 8601 UTC timestamp (nightly)")
    parser.add_argument("--tenant", help="tenant slug or uuid; default is every tenant (nightly)")
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
    if not args.config:
        parser.error("bootstrap needs --config")
    return _bootstrap(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
