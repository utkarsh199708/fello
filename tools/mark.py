# ruff: noqa: E501  (embedded HTML template and data tables read better unwrapped)
"""Mark tracker tasks. Safe for concurrent callers (exclusive lock file).

Usage:
  python tools/mark.py 1.2 in_progress "Data engineer started the generator"
  python tools/mark.py 1.2,1.3 done "Generator and schema committed (abc1234)"
  python tools/mark.py 2.8 cut "Second drift case cut for time"

Statuses: todo, in_progress, done, blocked, cut.
"done" also ticks the checkbox in docs/TASKS.md, the source of truth.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "docs" / "tracker_state.json"
TASKS = ROOT / "docs" / "TASKS.md"
LOCK = ROOT / "docs" / ".tracker.lock"
VALID = {"todo", "in_progress", "done", "blocked", "cut"}


def acquire(timeout: float = 60.0) -> int:
    end = time.time() + timeout
    while True:
        try:
            return os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > end:
                LOCK.unlink(missing_ok=True)  # stale lock from a crashed caller
            time.sleep(0.2)


def tick(ids: list[str]) -> None:
    text = TASKS.read_text(encoding="utf-8")
    for tid in ids:
        pat = re.compile(r"^- \[ \] (\*\*" + re.escape(tid) + r"[ .])", re.M)
        text = pat.sub(r"- [x] \1", text)
    TASKS.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] not in VALID:
        sys.exit(__doc__)
    ids = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    status, msg = sys.argv[2], sys.argv[3]
    fd = acquire()
    try:
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"status": {}, "log": []}
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for tid in ids:
            if status == "done":
                st["status"].pop(tid, None)
            else:
                st["status"][tid] = status
        st["log"].append({"at": now, "msg": f"{', '.join(ids)} {status.replace('_', ' ')}: {msg}"})
        st["updated"] = now
        STATE.write_text(json.dumps(st, indent=2), encoding="utf-8", newline="\n")
        if status == "done":
            tick(ids)
        sys.path.insert(0, str(ROOT / "tools"))
        import tracker  # noqa: E402

        tracker.render()
    finally:
        os.close(fd)
        LOCK.unlink(missing_ok=True)
    print(f"marked {ids} {status}")


if __name__ == "__main__":
    main()
