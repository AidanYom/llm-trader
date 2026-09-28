from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from trader.db import repo
from trader.db.repo import NewRun, ReportMalformed, ReportOrder, ReportRun
from trader.models import (
    AccountState,
    Order,
    OrderStatus,
    Position,
    Proposal,
    RunMode,
    RunStatus,
    Side,
    Usage,
    Verdict,
    VerdictStatus,
)

MONDAY = date(2026, 9, 28)
FRIDAY = date(2026, 10, 2)
WINDOW: dict[str, Any] = {
    "first_day": MONDAY,
    "last_day": FRIDAY,
    "paper": True,
    "modes": [RunMode.DRY_RUN, RunMode.SUBMIT],
}


def a_run(
    conn: Connection,
    day: date,
    *,
    mode: RunMode = RunMode.SUBMIT,
    paper: bool = True,
    status: RunStatus = RunStatus.COMPLETED,
    equity: float | None = 10_000.0,
    hour: int = 12,
) -> UUID:
    started_at = datetime.combine(day, time(hour, 31), tzinfo=UTC)
    run = NewRun(
        run_date=day,
        started_at=started_at,
        mode=mode,
        paper=paper,
        model="claude-sonnet-5",
        prompt_version=None,
    )
    if status is RunStatus.SKIPPED:
        return repo.record_skipped_run(conn, run, reason="market closed today")
    run_id = repo.start_run(conn, run)
    assert run_id is not None
    if equity is not None:
        repo.record_account_snapshot(
            conn, run_id, AccountState(equity=equity, cash=equity / 10), taken_at=started_at
        )
    finished_at = started_at + timedelta(minutes=2)
    if status is RunStatus.COMPLETED:
        repo.finish_run(
            conn,
            run_id,
            finished_at=finished_at,
            briefing="b",
            market_view="Energy leads.",
            agent_submitted=True,
            agent_turns=3,
            usage=Usage(cost_usd=0.05),
        )
    elif status is RunStatus.FAILED:
        repo.fail_run(
            conn, run_id, finished_at=finished_at, error="RuntimeError: boom", usage=Usage(cost_usd=0.02)
        )
    return run_id


def test_report_runs_are_the_windows_runs_for_one_account_and_the_modes_asked(conn: Connection) -> None:
    before = a_run(conn, MONDAY - timedelta(days=1))
    completed = a_run(conn, MONDAY, equity=10_000.0)
    failed = a_run(conn, MONDAY + timedelta(days=1), status=RunStatus.FAILED, equity=None)
    skipped = a_run(conn, MONDAY + timedelta(days=2), status=RunStatus.SKIPPED)
    dry = a_run(conn, FRIDAY, mode=RunMode.DRY_RUN, equity=10_100.5, hour=11)
    a_run(conn, FRIDAY, mode=RunMode.OFFLINE)
    a_run(conn, FRIDAY, paper=False, hour=13)
    a_run(conn, FRIDAY + timedelta(days=1))

    found = repo.report_runs(conn, **WINDOW)

    assert [run.run_id for run in found] == [completed, failed, skipped, dry]
    assert before not in [run.run_id for run in found]
    first = found[0]
    assert first == ReportRun(
        run_id=completed,
        run_date=MONDAY,
        started_at=datetime(2026, 9, 28, 12, 31, tzinfo=UTC),
        mode=RunMode.SUBMIT,
        status=RunStatus.COMPLETED,
        skip_reason=None,
        error=None,
        model="claude-sonnet-5",
        prompt_version=None,
        market_view="Energy leads.",
        agent_submitted=True,
        cost_usd=0.05,
        equity=10_000.0,
        cash=1_000.0,
    )
    assert (found[1].status, found[1].error, found[1].cost_usd, found[1].equity) == (
        RunStatus.FAILED,
        "RuntimeError: boom",
        0.02,
        None,
    )
    assert (found[2].status, found[2].skip_reason) == (RunStatus.SKIPPED, "market closed today")
    assert (found[3].equity, found[3].cash) == (10_100.5, 1_010.05)
    offline = repo.report_runs(conn, **(WINDOW | {"modes": [RunMode.OFFLINE]}))
    assert [run.mode for run in offline] == [RunMode.OFFLINE]


def test_report_details_of_a_run(conn: Connection) -> None:
    run_id = a_run(conn, MONDAY)
    buy = Order(symbol="URA", side=Side.BUY, qty=12, limit_price=40.4, stop_price=37.0)
    rejected = Verdict(
        symbol="TQQQ", action=Side.BUY, status=VerdictStatus.REJECTED, reasons=("blocklist: TQQQ is blocked",)
    )
    approved = Verdict(
        symbol="URA", action=Side.BUY, status=VerdictStatus.APPROVED, opens_new_position=True, order=buy
    )
    for seq, (symbol, verdict) in enumerate([("URA", approved), ("TQQQ", rejected)]):
        proposal = Proposal(
            symbol=symbol, action=Side.BUY, thesis="t", invalidation="i", target_pct=5.0, stop_pct=None
        )
        proposal_id = repo.record_proposal(conn, run_id, seq=seq, proposal=proposal, raw={}, verdict=verdict)
        if verdict.order is not None:
            repo.record_order(
                conn,
                run_id,
                proposal_id=proposal_id,
                client_order_id="llmt-2026-09-28-URA-buy",
                order=buy,
                opens_new_position=True,
                status=OrderStatus.SUBMITTED,
            )
    repo.record_malformed_proposal(
        conn, run_id, raw={"action": "hold"}, error="action: 'hold' is not buy or sell"
    )
    for seq, name in enumerate(["get_price_history", "get_news", "get_news", "submit_proposals"]):
        repo.record_tool_call(conn, run_id, seq=seq, name=name, tool_input={}, result=None)

    proposals = repo.report_proposals(conn, [run_id])

    assert [(p.seq, p.symbol, p.status, p.reasons) for p in proposals] == [
        (0, "URA", VerdictStatus.APPROVED, ()),
        (1, "TQQQ", VerdictStatus.REJECTED, ("blocklist: TQQQ is blocked",)),
    ]
    assert (proposals[0].action, proposals[0].target_pct, proposals[0].stop_pct) == (Side.BUY, 5.0, None)
    assert repo.report_orders(conn, [run_id]) == [
        ReportOrder(run_id=run_id, symbol="URA", side=Side.BUY, qty=12, status=OrderStatus.SUBMITTED)
    ]
    assert repo.report_malformed(conn, [run_id]) == [
        ReportMalformed(run_id=run_id, error="action: 'hold' is not buy or sell")
    ]
    assert repo.report_research_calls(conn, [run_id]) == {run_id: 3}  # submit_proposals isn't research
    assert repo.report_proposals(conn, []) == []


def test_report_positions_are_largest_first_with_unknowns_as_nan(conn: Connection) -> None:
    run_id = repo.start_run(
        conn,
        NewRun(
            run_date=MONDAY,
            started_at=datetime(2026, 9, 28, 12, 31, tzinfo=UTC),
            mode=RunMode.SUBMIT,
            paper=True,
            model="m",
            prompt_version=None,
        ),
    )
    assert run_id is not None
    held = (
        Position(
            symbol="XLE",
            qty=10,
            avg_entry_price=80.0,
            current_price=90.0,
            market_value=900.0,
            unrealized_plpc=0.125,
        ),
        Position(
            symbol="SMH",
            qty=5,
            avg_entry_price=200.0,
            current_price=math.nan,
            market_value=math.nan,
            unrealized_plpc=0.0,
        ),
        Position(
            symbol="URA",
            qty=40,
            avg_entry_price=40.0,
            current_price=41.0,
            market_value=1_640.0,
            unrealized_plpc=0.025,
        ),
    )
    repo.record_account_snapshot(
        conn,
        run_id,
        AccountState(equity=1.0, cash=1.0, positions=held),
        taken_at=datetime(2026, 9, 28, 12, 31, tzinfo=UTC),
    )

    positions = repo.report_positions(conn, run_id)

    assert [position.symbol for position in positions] == ["URA", "XLE", "SMH"]
    assert positions[1] == held[0]
    assert math.isnan(positions[2].market_value)
