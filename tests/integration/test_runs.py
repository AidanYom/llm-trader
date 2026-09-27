from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, select

from trader.db import repo
from trader.db.repo import NewRun, RunStateError
from trader.db.tables import prompt_versions, runs
from trader.models import RunMode, Usage

DAY = date(2026, 9, 28)  # a Monday
STARTED = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York
FINISHED = STARTED + timedelta(minutes=3)


def new_run(mode: RunMode = RunMode.SUBMIT, *, day: date = DAY, paper: bool = True) -> NewRun:
    return NewRun(
        run_date=day, started_at=STARTED, mode=mode, paper=paper, model="claude-sonnet-5", prompt_version=None
    )


def started(conn: Connection, run: NewRun | None = None) -> UUID:
    run_id = repo.start_run(conn, new_run() if run is None else run)
    assert run_id is not None
    return run_id


def complete(conn: Connection, run_id: UUID) -> None:
    repo.finish_run(
        conn,
        run_id,
        finished_at=FINISHED,
        briefing="# Daily briefing",
        market_view=None,
        agent_submitted=False,
        agent_turns=2,
        usage=Usage(),
    )


def row_of(conn: Connection, run_id: UUID) -> Any:  # a Row, read by column name
    return conn.execute(select(runs).where(runs.c.id == run_id)).one()


def test_start_run_inserts_a_running_row(conn: Connection) -> None:
    repo.save_prompt_version(conn, version="0123456789", system_prompt="You are the research agent.")
    run_id = started(conn, replace(new_run(), prompt_version="0123456789"))

    row = row_of(conn, run_id)
    assert (row.run_date, row.started_at, row.mode, row.paper, row.model, row.prompt_version) == (
        DAY,
        STARTED,
        "submit",
        True,
        "claude-sonnet-5",
        "0123456789",
    )
    assert (row.status, row.finished_at, row.input_tokens, row.cost_usd) == ("running", None, 0, Decimal(0))


def test_second_submit_run_on_a_day_is_refused(conn: Connection) -> None:
    started(conn)

    assert repo.start_run(conn, new_run()) is None
    assert len(conn.execute(select(runs.c.id)).all()) == 1


def test_one_submit_run_per_day_is_per_account_type_and_submit_mode_only(conn: Connection) -> None:
    started(conn)

    assert repo.start_run(conn, new_run(paper=False)) is not None  # the live account has its own day
    assert repo.start_run(conn, new_run(day=DAY + timedelta(days=1))) is not None
    for mode in (RunMode.DRY_RUN, RunMode.DRY_RUN, RunMode.OFFLINE, RunMode.OFFLINE):
        assert repo.start_run(conn, new_run(mode)) is not None


def test_submit_run_can_start_again_after_a_failure(conn: Connection) -> None:
    repo.fail_run(conn, started(conn), finished_at=FINISHED, error="RuntimeError: broker timeout")

    assert repo.start_run(conn, new_run()) is not None


def test_completed_submit_run_blocks_the_rest_of_the_day(conn: Connection) -> None:
    complete(conn, started(conn))

    assert repo.start_run(conn, new_run()) is None


def test_skipped_run_records_its_reason_and_blocks_nothing(conn: Connection) -> None:
    skipped = repo.record_skipped_run(conn, new_run(), reason="already ran in submit mode today")
    repo.record_skipped_run(conn, new_run(), reason="market closed today")

    row = row_of(conn, skipped)
    assert (row.status, row.skip_reason, row.finished_at) == (
        "skipped",
        "already ran in submit mode today",
        STARTED,
    )
    assert repo.start_run(conn, new_run()) is not None


def test_force_abandons_only_the_days_running_submit_run(conn: Connection) -> None:
    stale = started(conn)
    others = [
        started(conn, new_run(day=DAY - timedelta(days=1))),
        started(conn, new_run(paper=False)),
        started(conn, new_run(RunMode.DRY_RUN)),
    ]

    assert repo.abandon_stale_submit_run(conn, run_date=DAY, paper=True, at=FINISHED) == stale
    assert (row_of(conn, stale).status, row_of(conn, stale).finished_at) == ("abandoned", FINISHED)
    assert [row_of(conn, run_id).status for run_id in others] == ["running"] * 3
    assert repo.start_run(conn, new_run()) is not None


def test_force_never_reopens_a_completed_day(conn: Connection) -> None:
    complete(conn, started(conn))

    assert repo.abandon_stale_submit_run(conn, run_date=DAY, paper=True, at=FINISHED) is None
    assert repo.start_run(conn, new_run()) is None


def test_finish_run_writes_the_summary(conn: Connection) -> None:
    run_id = started(conn)
    usage = Usage(
        input_tokens=12_000,
        output_tokens=900,
        cache_write_tokens=8_000,
        cache_read_tokens=40_000,
        cost_usd=0.0612,
    )

    repo.finish_run(
        conn,
        run_id,
        finished_at=FINISHED,
        briefing="# Daily briefing\n- [Sep 28 07:02 ET] (XLE) headline\x00: summary",
        market_view="Energy leads on 1m relative strength.",
        agent_submitted=True,
        agent_turns=4,
        usage=usage,
    )

    row = row_of(conn, run_id)
    assert (row.status, row.finished_at, row.agent_submitted, row.agent_turns) == (
        "completed",
        FINISHED,
        True,
        4,
    )
    assert (
        row.briefing
        == "# Daily briefing\n- [Sep 28 07:02 ET] (XLE) headline\N{REPLACEMENT CHARACTER}: summary"
    )
    assert row.market_view == "Energy leads on 1m relative strength."
    tokens = (row.input_tokens, row.output_tokens, row.cache_write_tokens, row.cache_read_tokens)
    assert tokens == (12_000, 900, 8_000, 40_000)
    assert row.cost_usd == Decimal("0.0612")


def test_abandoned_run_cannot_complete(conn: Connection) -> None:
    run_id = started(conn)
    repo.abandon_stale_submit_run(conn, run_date=DAY, paper=True, at=FINISHED)

    with pytest.raises(RunStateError, match="is not running, so it can't be completed"):
        complete(conn, run_id)
    assert row_of(conn, run_id).status == "abandoned"


def test_fail_run_records_the_error_and_leaves_other_states_alone(conn: Connection) -> None:
    failing = started(conn, new_run(RunMode.DRY_RUN))
    abandoned = started(conn)
    repo.abandon_stale_submit_run(conn, run_date=DAY, paper=True, at=FINISHED)

    repo.fail_run(conn, failing, finished_at=FINISHED, error="RuntimeError: broker timeout")
    repo.fail_run(conn, abandoned, finished_at=FINISHED, error="too late")  # doesn't raise

    assert (row_of(conn, failing).status, row_of(conn, failing).error) == (
        "failed",
        "RuntimeError: broker timeout",
    )
    assert (row_of(conn, abandoned).status, row_of(conn, abandoned).error) == ("abandoned", None)


def test_prompt_version_is_stored_once(conn: Connection) -> None:
    repo.save_prompt_version(conn, version="0123456789", system_prompt="You are the research agent.")
    repo.save_prompt_version(conn, version="0123456789", system_prompt="You are the research agent.")

    assert conn.execute(select(prompt_versions.c.version)).scalars().all() == ["0123456789"]


def test_run_times_need_a_time_zone_and_run_dates_must_be_dates(conn: Connection) -> None:
    with pytest.raises(ValueError, match="has no time zone"):
        repo.start_run(conn, replace(new_run(), started_at=datetime(2026, 9, 28, 8, 31)))
    with pytest.raises(TypeError, match="expected an America/New_York date"):
        repo.start_run(conn, replace(new_run(), run_date=STARTED))
