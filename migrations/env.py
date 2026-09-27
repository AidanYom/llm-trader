"""Alembic's entry point (HANDOFF §10): runs the migrations against the database DATABASE_URL names.

The integration tests hand over their own connection in `config.attributes["connection"]` instead, so they
can migrate the test database inside a transaction.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from trader.db.engine import make_engine
from trader.db.tables import metadata

config = context.config


def main() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    if context.is_offline_mode():
        # `alembic upgrade head --sql` prints the SQL instead of running it.
        context.configure(dialect_name="postgresql", target_metadata=metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()


def migrate(connection: Connection) -> None:
    # compare_server_default: autogenerate and `alembic check` compare column defaults too.
    context.configure(connection=connection, target_metadata=metadata, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set: it names the database to migrate")
    return url


main()
