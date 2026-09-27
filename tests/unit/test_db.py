from __future__ import annotations

import traceback
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from trader.db.engine import DatabaseUrlError, make_engine, postgres_url
from trader.db.tables import SCHEMA_HEAD

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_schema_head_names_the_newest_migration() -> None:
    heads = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_heads()

    assert heads == [SCHEMA_HEAD], f"set SCHEMA_HEAD in src/trader/db/tables.py to the new revision: {heads}"


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg"])
def test_postgres_url_uses_psycopg(scheme: str) -> None:
    url = postgres_url(f"{scheme}://trader:s3cret@db.example.com:5432/trader?sslmode=require")

    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database) == ("db.example.com", 5432, "trader")
    assert url.query == {"sslmode": "require"}


def test_engine_uses_psycopg_without_connecting() -> None:
    engine = make_engine("postgresql://trader:s3cret@localhost:5432/trader")

    assert engine.dialect.driver == "psycopg"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("postgresql://trader:s3cret@host:notaport/trader", "can't be parsed"),
        ("not a url s3cret", "can't be parsed"),
        ("mysql://trader:s3cret@host/trader", "must be for Postgres, not mysql"),
    ],
)
def test_unusable_url_is_refused_without_repeating_it(url: str, message: str) -> None:
    with pytest.raises(DatabaseUrlError, match=message) as exc_info:
        postgres_url(url)

    # The whole traceback, as a log handler would print it, including any chained exception.
    assert "s3cret" not in "".join(traceback.format_exception(exc_info.value))
