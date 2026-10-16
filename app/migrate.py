"""``python -m app migrate``: create the database and roles, then upgrade the schema to head.

Two steps, because they need different users. ``prepare_database`` runs as the admin user from
``DATABASE_URL``: only a superuser can create a BYPASSRLS role. Alembic then runs as
``app_owner`` (see migrations/env.py), so that role owns every table. Both steps are idempotent,
so the worker runs this on every start.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.db import prepare_database

# alembic.ini sits at the repo root, next to the app package.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def migrate() -> None:
    """Create the database and roles if missing, then run ``alembic upgrade head``."""
    prepare_database()
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


def main(argv: list[str]) -> int:
    """Entry point for ``python -m app migrate``. ``argv[0]`` is the command name itself."""
    argparse.ArgumentParser(
        prog="python -m app migrate",
        description="Create the database and roles if missing, then upgrade the schema.",
    ).parse_args(argv[1:])
    migrate()
    print("migrated to head")
    return 0
