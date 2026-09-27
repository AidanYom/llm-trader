"""initial schema: every table in HANDOFF §10

Revision ID: a77d5ab72d60
Revises:
Create Date: 2026-09-27 21:59:58.640793

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "a77d5ab72d60"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_prompt_versions")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("paper", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("briefing", sa.Text(), nullable=True),
        sa.Column("market_view", sa.Text(), nullable=True),
        sa.Column("agent_submitted", sa.Boolean(), nullable=True),
        sa.Column("agent_turns", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=4), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("mode IN ('offline', 'dry_run', 'submit')", name=op.f("ck_runs_mode")),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'skipped', 'failed', 'abandoned')",
            name=op.f("ck_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version"],
            ["prompt_versions.version"],
            name=op.f("fk_runs_prompt_version_prompt_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(op.f("ix_runs_run_date"), "runs", ["run_date"], unique=False)
    op.create_index(
        "uq_runs_one_submit_per_day",
        "runs",
        ["run_date", "paper"],
        unique=True,
        postgresql_where=sa.text("mode = 'submit' AND status IN ('running', 'completed')"),
    )
    op.create_table(
        "account_snapshots",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("equity", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("cash", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_account_snapshots_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_account_snapshots")),
    )
    op.create_table(
        "cancelled_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("reason IN ('stale_entry', 'exit_legs')", name=op.f("ck_cancelled_orders_reason")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_cancelled_orders_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cancelled_orders")),
    )
    op.create_table(
        "malformed_proposals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_malformed_proposals_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_malformed_proposals")),
    )
    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("avg_entry_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("current_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("market_value", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("unrealized_plpc", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_position_snapshots_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_snapshots")),
        sa.UniqueConstraint("run_id", "symbol", name=op.f("uq_position_snapshots_run_id_symbol")),
    )
    op.create_table(
        "proposals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_pct", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("stop_pct", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("take_profit_pct", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("invalidation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("action IN ('buy', 'sell')", name=op.f("ck_proposals_action")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_proposals_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_proposals_run_id_seq")),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_tool_calls_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_tool_calls_run_id_seq")),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("take_profit_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("opens_new_position", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("not_submitted_reason", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("broker_status", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_orders_side")),
        sa.CheckConstraint(
            "status IN ('submitted', 'not_submitted', 'error')", name=op.f("ck_orders_status")
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], name=op.f("fk_orders_proposal_id_proposals")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_orders_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_table(
        "verdicts",
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("opens_new_position", sa.Boolean(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=True),
        sa.Column("limit_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("take_profit_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.CheckConstraint("status IN ('approved', 'trimmed', 'rejected')", name=op.f("ck_verdicts_status")),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], name=op.f("fk_verdicts_proposal_id_proposals")
        ),
        sa.PrimaryKeyConstraint("proposal_id", name=op.f("pk_verdicts")),
    )


def downgrade() -> None:
    op.drop_table("verdicts")
    op.drop_table("orders")
    op.drop_table("tool_calls")
    op.drop_table("proposals")
    op.drop_table("position_snapshots")
    op.drop_table("malformed_proposals")
    op.drop_table("cancelled_orders")
    op.drop_table("account_snapshots")
    op.drop_index("uq_runs_one_submit_per_day", table_name="runs")
    op.drop_index(op.f("ix_runs_run_date"), table_name="runs")
    op.drop_table("runs")
    op.drop_table("prompt_versions")
