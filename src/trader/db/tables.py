"""The database schema (HANDOFF §10), as SQLAlchemy Core tables.

`make revision` drafts a migration by comparing these tables with the dev database (CLAUDE.md, "Database and
migrations"). That comparison can't see check constraints or partial-index conditions, so
tests/integration/test_migrations.py also checks that the migrations build exactly these tables.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from trader.models import CancelReason, OrderStatus, RunMode, RunStatus, Side, VerdictStatus

# Names every constraint and index, so autogenerate compares them by name and migrations can drop them.
metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    }
)

# The runs that uq_runs_one_submit_per_day allows only one of per (run_date, paper) (HANDOFF §9).
ONE_SUBMIT_PER_DAY = text("mode = 'submit' AND status IN ('running', 'completed')")


def _one_of(column: str, values: type[StrEnum]) -> CheckConstraint:
    """A check that the column holds one of the enum's values, named ck_<table>_<column>."""
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def _id() -> Column[Any]:
    return Column("id", BigInteger, Identity(), primary_key=True)


def _run_id() -> Column[Any]:
    return Column("run_id", Uuid, ForeignKey("runs.id"), nullable=False)


def _timestamp(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


prompt_versions = Table(
    "prompt_versions",
    metadata,
    Column("version", Text, primary_key=True),  # the first 10 hex characters of the prompt's SHA-256
    Column("system_prompt", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("run_date", Date, nullable=False, index=True),  # the America/New_York date
    _timestamp("started_at"),
    _timestamp("finished_at", nullable=True),
    Column("mode", Text, nullable=False),
    Column("paper", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("skip_reason", Text),
    Column("error", Text),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, ForeignKey("prompt_versions.version")),
    Column("briefing", Text),
    Column("market_view", Text),
    Column("agent_submitted", Boolean),
    Column("agent_turns", Integer),
    Column("input_tokens", Integer, nullable=False, server_default=text("0")),
    Column("output_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cache_write_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cache_read_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cost_usd", Numeric(10, 4), nullable=False, server_default=text("0")),
    _one_of("mode", RunMode),
    _one_of("status", RunStatus),
    Index(
        "uq_runs_one_submit_per_day",
        "run_date",
        "paper",
        unique=True,
        postgresql_where=ONE_SUBMIT_PER_DAY,
    ),
)

# Numbers from the broker are nullable: repo.py stores a NaN or infinity as NULL, meaning unknown.
account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("run_id", Uuid, ForeignKey("runs.id"), primary_key=True),
    Column("equity", Numeric(14, 2)),
    Column("cash", Numeric(14, 2)),
    _timestamp("taken_at"),
)

position_snapshots = Table(
    "position_snapshots",
    metadata,
    _id(),
    _run_id(),
    Column("symbol", Text, nullable=False),
    Column("qty", Numeric(18, 6)),
    Column("avg_entry_price", Numeric(14, 4)),
    Column("current_price", Numeric(14, 4)),
    Column("market_value", Numeric(14, 2)),
    Column("unrealized_plpc", Numeric(10, 6)),  # a fraction of cost: 0.05 is +5%
    UniqueConstraint("run_id", "symbol"),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    _id(),
    _run_id(),
    Column("seq", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("input", JSONB, nullable=False),
    Column("result_excerpt", Text),  # the first 1,500 characters; null when no result was sent back
    UniqueConstraint("run_id", "seq"),
)

# Numbers from the model are nullable: the percentages are optional, and repo.py stores a NaN as NULL.
proposals = Table(
    "proposals",
    metadata,
    _id(),
    _run_id(),
    Column("seq", Integer, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_pct", Numeric(6, 2)),
    Column("stop_pct", Numeric(6, 2)),
    Column("take_profit_pct", Numeric(6, 2)),
    Column("thesis", Text, nullable=False),
    Column("invalidation", Text, nullable=False),
    Column("confidence", Numeric(4, 3)),
    Column("raw", JSONB, nullable=False),  # the proposal exactly as the model sent it
    _one_of("action", Side),
    UniqueConstraint("run_id", "seq"),
)

malformed_proposals = Table(
    "malformed_proposals",
    metadata,
    _id(),
    _run_id(),
    Column("raw", JSONB, nullable=False),
    Column("error", Text, nullable=False),
)

verdicts = Table(
    "verdicts",
    metadata,
    Column("proposal_id", BigInteger, ForeignKey("proposals.id"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("reasons", JSONB, nullable=False),  # an array of "category: detail" strings
    Column("opens_new_position", Boolean, nullable=False),
    Column("qty", Integer),  # the order's: null for a rejection
    Column("limit_price", Numeric(14, 2)),  # null for a rejection or a sell
    Column("stop_price", Numeric(14, 2)),
    Column("take_profit_price", Numeric(14, 2)),
    _one_of("status", VerdictStatus),
)

orders = Table(
    "orders",
    metadata,
    _id(),
    _run_id(),
    Column("proposal_id", BigInteger, ForeignKey("proposals.id"), nullable=False),
    Column("client_order_id", Text, nullable=False),  # llmt-{run_date}-{SYMBOL}-{side}
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("limit_price", Numeric(14, 2)),  # null for a sell: a market order
    Column("stop_price", Numeric(14, 2)),
    Column("take_profit_price", Numeric(14, 2)),
    Column("opens_new_position", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("not_submitted_reason", Text),
    Column("broker_order_id", Text),
    Column("broker_status", Text),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _one_of("side", Side),
    _one_of("status", OrderStatus),
)

cancelled_orders = Table(
    "cancelled_orders",
    metadata,
    _id(),
    _run_id(),
    Column("broker_order_id", Text, nullable=False),
    Column("symbol", Text),  # null when the broker call returned only the order's ID
    Column("reason", Text, nullable=False),
    _one_of("reason", CancelReason),
)
