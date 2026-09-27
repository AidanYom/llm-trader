"""Database connections: SQLAlchemy engines on the psycopg 3 driver (HANDOFF §10)."""

from __future__ import annotations

import json
from functools import partial

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.exc import ArgumentError


class DatabaseUrlError(ValueError):
    """The database URL can't be used. The message never repeats the URL: it contains the password."""


# JSON has no NaN or Infinity, and Postgres refuses them in JSONB. repo.py writes such floats as strings; a
# value that gets past it raises here, instead of reaching the database as invalid JSON.
_json_dumps = partial(json.dumps, allow_nan=False)


def make_engine(url: str) -> Engine:
    """An engine for a Postgres URL, such as DATABASE_URL."""
    return create_engine(postgres_url(url), json_serializer=_json_dumps, pool_pre_ping=True)


def postgres_url(url: str) -> URL:
    """The URL with the psycopg 3 driver.

    Accepts `postgresql://` as Neon gives it: left alone, SQLAlchemy would look for psycopg2, which isn't
    installed.
    """
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):  # a ValueError, for a bad port, quotes part of the URL
        raise DatabaseUrlError("the database URL can't be parsed") from None
    if parsed.drivername not in ("postgres", "postgresql", "postgresql+psycopg"):
        raise DatabaseUrlError(f"the database URL must be for Postgres, not {parsed.drivername}")
    return parsed.set(drivername="postgresql+psycopg")
