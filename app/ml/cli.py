"""Command line for the ML group: ``anchors``, ``train`` and ``score``.

    python -m app anchors --config <yaml> [--tenant <slug or uuid>]
    python -m app train   --config <yaml>
    python -m app score   --as-of <ts>    [--tenant <slug or uuid>]

``anchors`` writes weekly anchor snapshots from ``horizon_start + anchor_warmup_weeks`` to
``train_as_of``. ``train`` fits and activates the model (as ``app_batch``). ``score`` writes the
nightly snapshot at ``--as-of`` and then the scores, one tenant transaction at a time. All three
are safe to run again. Time comes only from the config or ``--as-of``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from app.config import load_config, parse_utc
from app.db import tenant_ids, tenant_tx


def _anchors(args: argparse.Namespace) -> int:
    from app.ml.labels import build_anchors

    cfg = load_config(args.config)
    start = cfg.horizon_start + timedelta(weeks=cfg.anchor_warmup_weeks)
    print(f"anchors from {start.isoformat()} to {cfg.train_as_of.isoformat()}")
    for tid in tenant_ids(args.tenant):
        with tenant_tx(tid) as conn:
            n = build_anchors(conn, tid, start, cfg.train_as_of)
        print(f"{tid}  anchor snapshots written={n}")
    return 0


def _train(args: argparse.Namespace) -> int:
    from app.ml.train import train

    version = train(load_config(args.config))
    print(f"active model {version}")
    return 0


def _score(args: argparse.Namespace) -> int:
    from app.ml.features import write_snapshots
    from app.ml.score import score_tenant

    as_of = parse_utc(args.as_of)
    print(f"scoring at {as_of.isoformat()}")
    for tid in tenant_ids(args.tenant):
        with tenant_tx(tid) as conn:
            snapshots = write_snapshots(conn, tid, as_of, "nightly")
            scores = score_tenant(conn, tid, as_of)
        print(f"{tid}  snapshots written={snapshots}  scores written={scores}")
    return 0


def main(argv: list[str]) -> int:
    """``argv[0]`` is the command name, as app/__main__.py passes it."""
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", choices=["anchors", "train", "score"])
    parser.add_argument("--config", help="config YAML (anchors, train)")
    parser.add_argument("--as-of", help="ISO 8601 UTC timestamp (score)")
    parser.add_argument("--tenant", help="tenant slug or uuid; default is every tenant")
    args = parser.parse_args(argv)
    if args.command in ("anchors", "train") and not args.config:
        parser.error(f"{args.command} needs --config")
    if args.command == "score" and not args.as_of:
        parser.error("score needs --as-of")
    return {"anchors": _anchors, "train": _train, "score": _score}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
