"""The README's local-development steps agree with compose.yaml and .env.example.

These are text checks, not runs: they catch the two ways the documented setup silently
reached the wrong database or no database at all (Step 1 review round 1):

- nothing loads ``.env`` for host-side commands, so each documented ``uv run`` of the app or
  the tests must pass ``--env-file .env`` itself;
- the Compose db host port was hard-coded, and when 5432 was taken by another program the
  container still reported healthy while host tools reached that other program. ``DB_PORT``
  now moves it, and the URLs in ``.env.example`` must use the same port.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent


def read_env_example() -> dict[str, str]:
    """``KEY=value`` lines of .env.example, surrounding quotes removed. Comments skipped."""
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def test_compose_db_host_port_comes_from_db_port():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["db"]["ports"] == ["127.0.0.1:${DB_PORT:-5432}:5432"]


def test_env_example_urls_use_db_port():
    env = read_env_example()
    port = int(env["DB_PORT"])
    assert make_url(env["DATABASE_URL"]).port == port
    assert make_url(env["TEST_DATABASE_URL"]).port == port


def test_readme_commands_load_env_file():
    """Every documented host-side run of the app or the tests loads .env explicitly."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
    commands = [
        line.split("#")[0].strip()
        for block in blocks
        for line in block.splitlines()
        if re.match(r"uv run .*\b(pytest|python -m app)\b", line.strip())
    ]
    assert commands, "the README documents no app or test commands"
    missing = [c for c in commands if not c.startswith("uv run --env-file .env ")]
    assert missing == []
