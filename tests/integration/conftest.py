"""Integration tests run against the Postgres database that TEST_DATABASE_URL names (HANDOFF §14).

The database is wiped and migrated to head once per test run, and every table is emptied before each test.
To protect real data, the fixtures refuse any database whose name doesn't end in `_test`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text

from trader.db.engine import make_engine, postgres_url
from trader.db.tables import metadata

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """The test database, wiped and migrated to head once per test run."""
    engine = make_engine(_test_database_url())
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        command.upgrade(_alembic_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection to the test database, with every table empty.

    Nothing a test does is kept unless it commits: closing the connection rolls back.
    """
    with engine.connect() as connection:
        tables = ", ".join(table.name for table in metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        connection.commit()
        yield connection


@pytest.fixture
def alembic_config(conn: Connection) -> Config:
    """Alembic's configuration, migrating through the test's connection instead of DATABASE_URL."""
    return _alembic_config(conn)


def _alembic_config(connection: Connection) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = connection
    return config


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set: integration tests need a Postgres database", pytrace=False)
    name = postgres_url(url).database or ""
    if not name.endswith("_test"):
        pytest.fail(f"refusing to wipe database {name!r}: its name must end in _test", pytrace=False)
    return url
