"""Scaffold smoke tests: the package imports, and CI reaches its Postgres 16 service."""

import os

import pytest
import sqlalchemy as sa

import app


def test_package_imports():
    assert app.__name__ == "app"


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set")
def test_database_is_postgres_16():
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("SHOW server_version_num")).scalar_one()
    finally:
        engine.dispose()
    assert version.startswith("16")
