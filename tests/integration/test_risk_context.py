from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Connection

from trader.db import repo
from trader.db.repo import NewRun
from trader.models import (
    AccountState,
    Order,
    OrderStatus,
    Proposal,
    RiskContext,
    RunMode,
    RunStatus,
    Side,
    Usage,
    Verdict,
    VerdictStatus,
)

FRIDAY = date(2026, 9, 25)
SUNDAY_BEFORE = date(2026, 9, 27)
MONDAY = date(2026, 9, 28)
TUESDAY = date(2026, 9, 29)
WEDNESDAY = date(2026, 9, 30)
SUNDAY = date(2026, 10, 4)
NEXT_MONDAY = date(2026, 10, 5)

OPENED = (OrderStatus.SUBMITTED, True)  # the entry for a new position, accepted by the broker
EMPTY = RiskContext(new_positions_this_week=0, equity_peak=None)


def past_run(
    conn: Connection,
    day: date,
    *,
    mode: RunMode = RunMode.SUBMIT,
    paper: bool = True,
    status: RunStatus = RunStatus.COMPLETED,
    equity: float | None = None,
    buys: Sequence[tuple[OrderStatus, bool]] = (),
) -> None:
    """A run dated `day`, left in `status`.

    It has an account snapshot when `equity` is given, and one buy order per (order status, whether it
    opens a new position) in `buys`.
    """
    started_at = datetime.combine(day, time(12, 31), tzinfo=UTC)
    run = NewRun(run_date=day, started_at=started_at, mode=mode, paper=paper, model="m", prompt_version=None)
    run_id = repo.start_run(conn, run)
    assert run_id is not None
    if equity is not None:
        repo.record_account_snapshot(
            conn, run_id, AccountState(equity=equity, cash=equity), taken_at=started_at
        )
    for seq, (order_status, opens_new) in enumerate(buys):
        symbol = f"SYM{seq}"
        order = Order(symbol=symbol, side=Side.BUY, qty=10, limit_price=50.5, stop_price=47.5)
        proposal = Proposal(
            symbol=symbol, action=Side.BUY, thesis="t", invalidation="i", target_pct=1.0, stop_pct=5.0
        )
        verdict = Verdict(
            symbol=symbol,
            action=Side.BUY,
            status=VerdictStatus.APPROVED,
            opens_new_position=opens_new,
            order=order,
        )
        proposal_id = repo.record_proposal(
            conn, run_id, seq=seq, proposal=proposal, raw={"symbol": symbol}, verdict=verdict
        )
        repo.record_order(
            conn,
            run_id,
            proposal_id=proposal_id,
            client_order_id=f"llmt-{day}-{symbol}-buy",
            order=order,
            opens_new_position=opens_new,
            status=order_status,
        )
    finished_at = started_at + timedelta(minutes=3)
    if status == RunStatus.COMPLETED:
        repo.finish_run(
            conn,
            run_id,
            finished_at=finished_at,
            briefing="b",
            market_view=None,
            agent_submitted=True,
            agent_turns=1,
            usage=Usage(),
        )
    elif status == RunStatus.FAILED:
        repo.fail_run(conn, run_id, finished_at=finished_at, error="RuntimeError: boom")
    elif status == RunStatus.ABANDONED:
        assert repo.abandon_stale_submit_run(conn, run_date=day, paper=paper, at=finished_at) == run_id
    # RUNNING: left as it is, like a run that crashed before it could be marked failed.


def context(
    conn: Connection, run_date: date, *, paper: bool = True, peak_since: date | None = None
) -> RiskContext:
    return repo.risk_context(conn, run_date=run_date, paper=paper, peak_since=peak_since)


def test_no_history_gives_an_empty_context(conn: Connection) -> None:
    assert context(conn, MONDAY) == EMPTY


def test_weekly_count_resets_on_monday(conn: Connection) -> None:
    days = (FRIDAY, SUNDAY_BEFORE, MONDAY, TUESDAY, WEDNESDAY, SUNDAY)
    assert [day.weekday() for day in days] == [4, 6, 0, 1, 2, 6]
    past_run(conn, FRIDAY, buys=[OPENED, OPENED])
    past_run(conn, SUNDAY_BEFORE, buys=[OPENED])  # markets are closed, but the date still ends its week
    past_run(conn, MONDAY, buys=[OPENED])
    past_run(conn, TUESDAY, buys=[OPENED])

    assert context(conn, FRIDAY).new_positions_this_week == 2
    assert context(conn, SUNDAY_BEFORE).new_positions_this_week == 3
    assert context(conn, MONDAY).new_positions_this_week == 1  # Friday's and Sunday's belong to last week
    assert context(conn, WEDNESDAY).new_positions_this_week == 2
    assert context(conn, SUNDAY).new_positions_this_week == 2  # the week runs Monday to Sunday
    assert context(conn, NEXT_MONDAY).new_positions_this_week == 0


def test_only_submitted_orders_that_open_a_position_count(conn: Connection) -> None:
    past_run(
        conn,
        MONDAY,
        buys=[
            OPENED,
            (OrderStatus.SUBMITTED, False),  # adding to a holding
            (OrderStatus.NOT_SUBMITTED, True),  # held back: never sent
            (OrderStatus.ERROR, True),  # the broker refused it
        ],
    )

    assert context(conn, WEDNESDAY).new_positions_this_week == 1


def test_peak_is_the_highest_equity_of_any_dry_or_submit_run(conn: Connection) -> None:
    past_run(conn, FRIDAY, equity=100_000.0)
    past_run(conn, MONDAY, mode=RunMode.DRY_RUN, equity=104_000.5)
    past_run(conn, TUESDAY, equity=101_000.0)

    assert context(conn, WEDNESDAY) == RiskContext(new_positions_this_week=0, equity_peak=104_000.5)


def test_paper_and_live_accounts_are_counted_apart(conn: Connection) -> None:
    past_run(conn, MONDAY, equity=100_000.0, buys=[OPENED])
    past_run(conn, MONDAY, paper=False, equity=150_000.0, buys=[OPENED, OPENED])

    assert context(conn, WEDNESDAY) == RiskContext(new_positions_this_week=1, equity_peak=100_000.0)
    assert context(conn, WEDNESDAY, paper=False) == RiskContext(
        new_positions_this_week=2, equity_peak=150_000.0
    )


def test_peak_ignores_equity_before_drawdown_peak_since(conn: Connection) -> None:
    past_run(conn, FRIDAY, equity=120_000.0)
    past_run(conn, MONDAY, equity=105_000.0)
    past_run(conn, TUESDAY, equity=100_000.0)

    assert context(conn, WEDNESDAY).equity_peak == 120_000.0
    assert context(conn, WEDNESDAY, peak_since=MONDAY).equity_peak == 105_000.0  # the date itself counts
    assert context(conn, WEDNESDAY, peak_since=WEDNESDAY).equity_peak is None


def test_offline_runs_are_ignored(conn: Connection) -> None:
    past_run(conn, MONDAY, mode=RunMode.OFFLINE, equity=500_000.0, buys=[OPENED, OPENED])

    assert context(conn, WEDNESDAY) == EMPTY


def test_runs_that_failed_or_never_finished_still_count(conn: Connection) -> None:
    # Their orders were really placed, and their snapshots are real account reads.
    past_run(conn, MONDAY, status=RunStatus.FAILED, equity=110_000.0, buys=[OPENED])
    past_run(conn, MONDAY, status=RunStatus.ABANDONED, equity=100_000.0, buys=[OPENED])
    past_run(conn, TUESDAY, status=RunStatus.RUNNING, equity=100_000.0, buys=[OPENED])

    assert context(conn, WEDNESDAY) == RiskContext(new_positions_this_week=3, equity_peak=110_000.0)


def test_runs_dated_after_the_run_date_are_ignored(conn: Connection) -> None:
    past_run(conn, TUESDAY, equity=130_000.0, buys=[OPENED])

    assert context(conn, MONDAY) == EMPTY


def test_unknown_equity_is_not_a_peak(conn: Connection) -> None:
    past_run(conn, FRIDAY, equity=100_000.0)
    past_run(conn, MONDAY, equity=float("nan"))  # stored as NULL

    assert context(conn, WEDNESDAY).equity_peak == 100_000.0
