"""The offline scenario, as `make offline` runs it (HANDOFF §18: M3 is done when it writes every table)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, select

from trader.__main__ import main
from trader.db.tables import metadata, runs
from trader.models import RunMode, RunStatus
from trader.offline import offline_broker, offline_client
from trader.run import RunResult, run_daily
from trader.settings import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
MONDAY = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York
SUNDAY = datetime(2026, 9, 27, 12, 31, tzinfo=UTC)


def offline_run(engine: Engine, now: datetime) -> RunResult:
    return run_daily(
        mode=RunMode.OFFLINE,
        config=load_config(REPO_ROOT),  # the shipped config, as make offline uses it
        engine=engine,
        broker=offline_broker(now),
        client=offline_client(),
        clock=lambda: now,
    )


def test_an_offline_run_writes_every_table(engine: Engine, conn: Connection) -> None:
    result = offline_run(engine, MONDAY)

    assert result.status is RunStatus.COMPLETED
    empty = [
        table.name
        for table in metadata.sorted_tables
        if conn.execute(select(func.count()).select_from(table)).scalar_one() == 0
    ]
    assert empty == []


def test_offline_runs_work_at_weekends_and_give_the_same_result(engine: Engine, conn: Connection) -> None:
    first = offline_run(engine, SUNDAY)
    second = offline_run(engine, SUNDAY)

    assert first.status is second.status is RunStatus.COMPLETED
    assert first.summary == second.summary  # no risk context, so nothing carries over between offline runs
    assert first.summary.startswith("2026-09-27 · offline (paper) · completed")


def test_trader_run_offline_end_to_end(
    engine: Engine, conn: Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    monkeypatch.chdir(REPO_ROOT)

    assert main(["run", "--mode", "offline", "--show-briefing"]) == 0

    out = capsys.readouterr().out
    assert "# Daily briefing: " in out
    assert " · offline (paper) · completed" in out
    assert conn.execute(select(runs.c.mode, runs.c.status)).all() == [("offline", "completed")]
