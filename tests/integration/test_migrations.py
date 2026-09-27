from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from trader.db.tables import SCHEMA_HEAD, metadata


def test_migrations_round_trip(conn: Connection, alembic_config: Config) -> None:
    # One transaction: if a step fails, everything rolls back and the other tests still find the schema.
    with conn.begin():
        command.downgrade(alembic_config, "base")
        assert relations(conn) == {"alembic_version", "alembic_version_pkc"}  # Alembic's own table stays
        assert revisions(conn) == []

        command.upgrade(alembic_config, "head")

    assert revisions(conn) == [SCHEMA_HEAD]
    assert relations(conn) > {table.name for table in metadata.sorted_tables}


def test_migrated_schema_matches_tables_py(conn: Connection) -> None:
    """The migrations build exactly what tables.py describes.

    `alembic check` compares columns, types, defaults, keys and which indexes exist, but not check
    constraints or the WHERE clause of a partial index. This compares everything Postgres records.
    """
    conn.execute(text("CREATE SCHEMA tables_py"))  # rolled back with the rest of the test
    metadata.create_all(conn.execution_options(schema_translate_map={None: "tables_py"}))

    built, migrated = catalog(conn, "tables_py"), catalog(conn, "public")
    differences = {
        f"{kind} {where}": rows
        for kind in migrated
        for where, rows in (
            ("only in tables.py", built[kind] - migrated[kind]),
            ("only in the migrations", migrated[kind] - built[kind]),
        )
        if rows
    }
    assert differences == {}
    # The comparison is only as good as its contents: make sure the tricky parts are in there.
    assert {row[0] for row in migrated["columns"]} == set(metadata.tables)
    assert any("CHECK" in row[2] for row in migrated["constraints"])
    assert any("WHERE" in row[2] for row in migrated["indexes"])


@pytest.mark.parametrize(
    ("url", "message"),
    [
        # Nothing listens on port 1, so even a broken check couldn't wipe anything.
        ("postgresql+psycopg://trader:trader@127.0.0.1:1/trader", "refusing to wipe database 'trader'"),
        ("", "TEST_DATABASE_URL is not set"),
    ],
)
def test_fixtures_refuse_a_database_not_named_for_tests(url: str, message: str) -> None:
    round_trip = f"{__file__}::test_migrations_round_trip"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", round_trip],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "TEST_DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert message in result.stdout


def relations(conn: Connection) -> set[str]:
    """The names of every table, index and sequence in the public schema."""
    query = text(
        "SELECT relname FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace"
        " WHERE nspname = 'public'"
    )
    return set(conn.execute(query).scalars())


def revisions(conn: Connection) -> list[str]:
    return list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def catalog(conn: Connection, schema: str) -> dict[str, set[tuple[str, ...]]]:
    """What Postgres records about a schema's tables, with the schema's name taken out of definitions."""

    def rows(query: str) -> set[tuple[str, ...]]:
        result = conn.execute(text(query), {"schema": schema})
        return {tuple(str(value).replace(f"{schema}.", "") for value in row) for row in result}

    return {
        "columns": rows(
            "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale, is_nullable,"
            " column_default, is_identity, identity_generation"
            " FROM information_schema.columns"
            " WHERE table_schema = :schema AND table_name <> 'alembic_version'"
        ),
        "constraints": rows(
            "SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)"
            " FROM pg_constraint con"
            " JOIN pg_class rel ON rel.oid = con.conrelid"
            " JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace"
            " WHERE nsp.nspname = :schema AND rel.relname <> 'alembic_version'"
        ),
        "indexes": rows(
            "SELECT tablename, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = :schema AND tablename <> 'alembic_version'"
        ),
    }
