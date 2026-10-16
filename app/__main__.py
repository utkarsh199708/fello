"""One command line for every process: `python -m app <command> [args]`.

COMMANDS maps a command name to the module that runs it. A module is imported only when its
command is chosen, so `python -m app web` never loads pandas or scikit-learn.

Contract for command modules: each defines `main(argv: list[str]) -> int`. `argv` is the full
argument list after `python -m app`, command name first (for example `["load", "--data", "data"]`).
Several commands share a module (`load` and `ingest`, `anchors`, `train` and `score`), so the
module needs the name to know which one was asked for. A module that serves a single command
simply ignores `argv[0]`.
"""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "data": "app.generator.cli",
    "migrate": "app.migrate",
    "load": "app.ingest.cli",
    "ingest": "app.ingest.cli",
    "anchors": "app.ml.cli",
    "train": "app.ml.cli",
    "score": "app.ml.cli",
    "nightly": "app.jobs.cli",
    "bootstrap": "app.jobs.cli",
    "worker": "app.jobs.worker",
    "web": "app.web.main",
}


def usage() -> str:
    return "usage: python -m app <command> [args]\ncommands: " + ", ".join(COMMANDS)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0 if argv else 2
    command = argv[0]
    if command not in COMMANDS:
        print(f"unknown command {command!r}\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[command])
    return int(module.main(argv))


if __name__ == "__main__":
    sys.exit(main())
