"""Command line for the ingest group: ``load`` and ``ingest``.

    python -m app load   [--data data] [--tenant <slug or uuid>]
    python -m app ingest --as-of <ts>  [--tenant <slug or uuid>]

``load`` copies the generated files into Postgres. ``ingest`` runs only the resolve step, for the
events received by ``--as-of``. Both are safe to run again.
"""

from __future__ import annotations

import argparse
import sys

from app.config import parse_utc
from app.db import tenant_ids, tenant_tx
from app.ingest.loader import load_all
from app.ingest.resolve import ingest_watermark, resolve


def _load(args: argparse.Namespace) -> int:
    result = load_all(args.data, args.tenant)
    for table, n in result["reference"].items():
        print(f"reference  {table:<10} {n:>8} rows inserted")
    for slug, n in result["events"].items():
        print(f"raw_events {slug:<10} {n:>8} rows inserted")
    return 0


def _ingest(args: argparse.Namespace) -> int:
    as_of = parse_utc(args.as_of)
    print(f"resolving events received up to {as_of.isoformat()}")
    for tid in tenant_ids(args.tenant):
        with tenant_tx(tid) as conn:
            counts = resolve(conn, tid, as_of)
            watermark = ingest_watermark(conn, as_of)
        print(
            f"{tid}  normalised={counts.normalised} {counts.by_method}"
            f"  quarantined={counts.quarantined} {counts.by_reason}"
            f"  watermark={watermark.isoformat() if watermark else None}"
        )
    return 0


def main(argv: list[str]) -> int:
    """``argv[0]`` is the command name (``load`` or ``ingest``), as app/__main__.py passes it."""
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", choices=["load", "ingest"])
    parser.add_argument("--tenant", help="tenant slug or uuid; default is every tenant")
    parser.add_argument("--data", default="data", help="generated data directory (load only)")
    parser.add_argument("--as-of", help="ISO 8601 UTC timestamp (ingest only)")
    args = parser.parse_args(argv)
    if args.command == "ingest":
        if not args.as_of:
            parser.error("ingest needs --as-of")
        return _ingest(args)
    return _load(args)


if __name__ == "__main__":
    sys.exit(main(["load", *sys.argv[1:]]))
