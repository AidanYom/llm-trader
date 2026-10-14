"""The Lambda handler end to end, against the test database, FakeBroker and ScriptedClient (HANDOFF §16)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, func, select

from trader import lambda_handler
from trader.db.tables import orders, runs
from trader.lambda_handler import handler
from trader.offline import offline_broker, offline_client
from trader.run import run_daily
from trader.settings import Secrets

MONDAY = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York


def test_a_scheduled_invocation_records_a_completed_submit_run(
    engine: Engine, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = offline_broker(MONDAY)
    secrets = {"DATABASE_URL": os.environ["TEST_DATABASE_URL"], "ANTHROPIC_API_KEY": "anthropic-key-not-real"}
    monkeypatch.setattr(lambda_handler, "SECRETS", Secrets(secrets))
    monkeypatch.setattr(lambda_handler, "_alpaca", lambda secrets: broker)
    monkeypatch.setattr(lambda_handler, "anthropic_client", lambda api_key: offline_client())
    monkeypatch.setattr(lambda_handler, "run_daily", partial(run_daily, clock=lambda: MONDAY))
    monkeypatch.setenv("RUN_MODE", "submit")

    result = handler({}, None)  # the schedule's empty event

    assert result["status"] == "completed"
    row = conn.execute(select(runs.c.mode, runs.c.status).where(runs.c.id == UUID(result["run_id"]))).one()
    assert tuple(row) == ("submit", "completed")
    sent = conn.execute(select(func.count()).select_from(orders).where(orders.c.status == "submitted"))
    assert sent.scalar_one() == len(broker.submitted) > 0
