from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from trader.db.repo import ReportMalformed, ReportOrder, ReportProposal, ReportRun
from trader.models import Bar, OrderStatus, Position, RunMode, RunStatus, Side, VerdictStatus
from trader.report import (
    Baseline,
    EquityPoint,
    ReportData,
    average_invested,
    baseline_return,
    equity_series,
    peak_and_worst_drawdown,
    render,
)

MONDAY = date(2026, 9, 28)
BASKET = ("XLK", "XLE")


def bar(day: date, close: float) -> Bar:
    return Bar(day=day, open=close, high=close, low=close, close=close, volume=1_000_000)


def a_run(day: date, **changes: Any) -> ReportRun:
    fields: dict[str, Any] = {
        "run_id": uuid4(),
        "run_date": day,
        "started_at": datetime.combine(day, time(12, 31), tzinfo=UTC),
        "mode": RunMode.SUBMIT,
        "status": RunStatus.COMPLETED,
        "skip_reason": None,
        "error": None,
        "model": "claude-sonnet-5",
        "prompt_version": "0123456789",
        "market_view": "Energy leads.",
        "agent_submitted": True,
        "cost_usd": 0.05,
        "equity": 10_000.0,
        "cash": None,
    }
    return ReportRun(**(fields | changes))


def proposal(run_id: UUID, seq: int, symbol: str, status: VerdictStatus, *reasons: str) -> ReportProposal:
    return ReportProposal(
        run_id=run_id,
        seq=seq,
        symbol=symbol,
        action=Side.BUY,
        target_pct=5.0,
        stop_pct=8.0,
        thesis=f"{symbol} thesis.",
        invalidation=f"{symbol} invalidation.",
        status=status,
        reasons=reasons,
    )


def report(runs: list[ReportRun], **changes: Any) -> ReportData:
    fields: dict[str, Any] = {
        "first_day": MONDAY,
        "last_day": MONDAY + timedelta(days=6),
        "paper": True,
        "offline": False,
        "runs": runs,
        "proposals": [],
        "malformed": [],
        "orders": [],
        "research_calls": {},
        "positions": [],
        "baseline": Baseline(basket=BASKET, missing="skipped (--no-baseline)"),
    }
    return ReportData(**(fields | changes))


def section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading)) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [line for line in lines[start:end] if line]


# ---- Math --------------------------------------------------------------------------------------------------


def test_baseline_is_an_equal_weight_buy_and_hold_between_the_closes_before_the_run_dates() -> None:
    friday, monday, tuesday = date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)
    bars = {
        "XLK": [bar(friday, 100.0), bar(monday, 104.0), bar(tuesday, 999.0)],
        "XLE": [bar(friday, 50.0), bar(monday, 49.0), bar(tuesday, 999.0)],
    }

    # From Friday's close (before Monday's run) to Monday's close (before Tuesday's run).
    assert baseline_return(bars, BASKET, monday, tuesday) == pytest.approx((0.04 + -0.02) / 2)


def test_baseline_needs_every_etf() -> None:
    bars = {"XLK": [bar(date(2026, 9, 25), 100.0), bar(date(2026, 9, 28), 101.0)]}

    assert baseline_return(bars, BASKET, date(2026, 9, 28), date(2026, 9, 29)) is None


def test_equity_series_has_one_point_a_day_from_completed_runs() -> None:
    runs = [
        a_run(MONDAY, equity=10_000.0, mode=RunMode.DRY_RUN),
        a_run(MONDAY, equity=10_001.0),  # started later the same day: it wins
        a_run(MONDAY + timedelta(days=1), status=RunStatus.FAILED, equity=9_000.0),
        a_run(MONDAY + timedelta(days=2), equity=None),
        a_run(MONDAY + timedelta(days=3), equity=10_200.0),
    ]

    assert equity_series(runs) == [
        EquityPoint(day=MONDAY, equity=10_001.0, cash=None),
        EquityPoint(day=MONDAY + timedelta(days=3), equity=10_200.0, cash=None),
    ]


def test_average_invested_skips_points_without_cash_or_equity() -> None:
    points = [
        EquityPoint(day=MONDAY, equity=10_000.0, cash=500.0),  # 95% invested
        EquityPoint(day=MONDAY + timedelta(days=1), equity=10_000.0, cash=None),
        EquityPoint(day=MONDAY + timedelta(days=2), equity=0.0, cash=0.0),
        EquityPoint(day=MONDAY + timedelta(days=3), equity=8_000.0, cash=1_600.0),  # 80% invested
    ]

    assert average_invested(points) == pytest.approx(0.875)
    assert average_invested(points[1:3]) is None


def test_peak_and_worst_drawdown() -> None:
    peak, worst = peak_and_worst_drawdown([100.0, 110.0, 88.0, 105.0, 120.0, 108.0])

    assert peak == 120.0
    assert worst == pytest.approx(0.2)  # 110 down to 88


# ---- Rendering ---------------------------------------------------------------------------------------------


def test_an_empty_window_says_so() -> None:
    assert render(report([])) == (
        "# Weekly review: 2026-09-28 to 2026-10-04 (paper account)\n\n"
        "No dry_run or submit runs in this window.\n"
    )
    assert "(offline runs)" in render(report([], offline=True))
    assert "(live account)" in render(report([], paper=False))


def test_scorecard() -> None:
    runs = [
        a_run(MONDAY, equity=10_000.0, cash=1_000.0, cost_usd=0.10),
        a_run(MONDAY + timedelta(days=1), equity=11_000.0, cash=2_200.0, cost_usd=0.10),
        a_run(MONDAY + timedelta(days=2), status=RunStatus.FAILED, equity=None, cost_usd=0.05, error="Boom"),
        a_run(MONDAY + timedelta(days=3), equity=9_900.0, cash=990.0, cost_usd=0.10),
        a_run(MONDAY + timedelta(days=4), equity=10_500.0, cash=1_050.0, cost_usd=0.15),
        a_run(MONDAY + timedelta(days=5), status=RunStatus.SKIPPED, equity=None, cost_usd=0.0),
        a_run(MONDAY + timedelta(days=5), mode=RunMode.DRY_RUN, equity=None, agent_submitted=False),
    ]
    baseline = Baseline(basket=BASKET, start=MONDAY, end=MONDAY + timedelta(days=4), value=0.02)

    lines = section(render(report(runs, baseline=baseline)), "## Scorecard")

    assert lines[:4] == [
        "| Mode | Running | Completed | Skipped | Failed | Abandoned |",
        "|---|---:|---:|---:|---:|---:|",
        "| dry_run | 0 | 1 | 0 | 0 | 0 |",
        "| submit | 0 | 4 | 1 | 1 | 0 |",
    ]
    assert lines[4:] == [
        "- Equity: $10,000.00 on 2026-09-28 → $10,500.00 on 2026-10-02 (+5.0%)",
        "- Average invested: 87.5% of equity (the baseline is 100%)",  # 90%, 80%, 90% and 90%
        "- Peak equity $11,000.00; worst drawdown in the window 10.0%",
        "- API cost: $0.55 (0.01% of equity)",  # failed runs' cost included
        "- Baseline (equal-weight XLK, XLE), close before 2026-09-28 to close before 2026-10-02: +2.0%; "
        "the account's excess: +3.0 points",
    ]


def test_scorecard_without_a_baseline_or_equity() -> None:
    runs = [a_run(MONDAY, equity=None)]
    missing = Baseline(basket=BASKET, missing="n/a: no completed run with an equity snapshot")

    lines = section(render(report(runs, baseline=missing)), "## Scorecard")

    assert lines[3:] == [
        "- Equity: n/a (no completed run with an equity snapshot)",
        "- API cost: $0.05",
        "- Baseline (equal-weight XLK, XLE): n/a: no completed run with an equity snapshot",
    ]


def test_behavior() -> None:
    first, second = a_run(MONDAY), a_run(MONDAY + timedelta(days=1), agent_submitted=False)
    failed = a_run(MONDAY + timedelta(days=2), status=RunStatus.FAILED)
    third = a_run(MONDAY + timedelta(days=3))  # submitted, so the never-submitted count can't be flipped
    proposals = [
        proposal(first.run_id, 0, "URA", VerdictStatus.APPROVED),
        proposal(first.run_id, 1, "XLE", VerdictStatus.TRIMMED, "cash: $900.00 trimmed to $500.00 available"),
        proposal(first.run_id, 2, "TQQQ", VerdictStatus.REJECTED, "blocklist: TQQQ is blocked"),
        proposal(first.run_id, 3, "SQQQ", VerdictStatus.REJECTED, "blocklist: SQQQ is blocked"),
        proposal(
            first.run_id, 4, "ZZZ", VerdictStatus.REJECTED, "market data: no usable price history for ZZZ"
        ),
    ]
    orders = [
        ReportOrder(run_id=first.run_id, symbol="URA", side=Side.BUY, qty=10, status=OrderStatus.SUBMITTED),
        ReportOrder(run_id=first.run_id, symbol="XLE", side=Side.BUY, qty=5, status=OrderStatus.ERROR),
    ]

    lines = section(
        render(
            report(
                [first, second, failed, third],
                proposals=proposals,
                orders=orders,
                malformed=[ReportMalformed(run_id=first.run_id, error="action: 'hold' is not buy or sell")],
                research_calls={first.run_id: 6, failed.run_id: 40, third.run_id: 3},
            )
        ),
        "## Behavior",
    )

    assert lines == [
        "- Proposals: 1 approved, 1 trimmed, 3 rejected; 1 malformed",
        "- Runs where the model never submitted: 1",
        "- Orders: 1 submitted, 0 not_submitted, 1 error",
        "- Research tool calls per run: 3.0 on average, 6 at most",  # (6 + 0 + 3) / 3: completed runs only
        "- Prompt versions: 0123456789 (3 runs); models: claude-sonnet-5 (3 runs)",
        "- Top rejection reasons: blocklist (2), market data (1)",
    ]


def test_current_positions_come_from_the_last_snapshot() -> None:
    runs = [a_run(MONDAY), a_run(MONDAY + timedelta(days=1)), a_run(MONDAY + timedelta(days=2), equity=None)]
    held = [
        Position(
            symbol="URA",
            qty=12,
            avg_entry_price=40.0,
            current_price=42.0,
            market_value=504.0,
            unrealized_plpc=0.05,
        )
    ]

    assert section(render(report(runs, positions=held)), "## Current positions") == [
        "| Symbol | Qty | Avg entry | Last | P&L % | Market value |",
        "|---|---:|---:|---:|---:|---:|",
        "| URA | 12 | $40.00 | $42.00 | +5.0% | $504.00 |",
    ]
    assert "## Current positions (from the 2026-09-29 snapshot)" in render(report(runs, positions=held))
    assert section(render(report(runs)), "## Current positions") == ["No open positions."]


def test_daily_log() -> None:
    first = a_run(MONDAY, cost_usd=0.0612)
    silent = a_run(MONDAY + timedelta(days=1), agent_submitted=False, market_view=None)
    failed = a_run(
        MONDAY + timedelta(days=2), status=RunStatus.FAILED, cost_usd=0.02, error="RuntimeError: boom"
    )
    skipped = a_run(MONDAY + timedelta(days=5), status=RunStatus.SKIPPED, skip_reason="market closed today")
    proposals = [proposal(first.run_id, 0, "TQQQ", VerdictStatus.REJECTED, "blocklist: TQQQ is blocked")]

    lines = section(
        render(
            report(
                [first, silent, failed, skipped],
                proposals=proposals,
                malformed=[ReportMalformed(run_id=first.run_id, error="action: 'hold' is not buy or sell")],
            )
        ),
        "## Daily log",
    )

    assert lines == [
        "### 2026-09-28 · submit · $0.0612",
        "Energy leads.",
        "- buy TQQQ 5% (stop 8%): rejected · blocklist: TQQQ is blocked",
        "  - Thesis: TQQQ thesis.",
        "  - Invalidation: TQQQ invalidation.",
        "- Malformed: action: 'hold' is not buy or sell",
        "### 2026-09-29 · submit · $0.0500",
        "The model never called submit_proposals.",
        "### 2026-09-30 · submit · failed · $0.0200",
        "RuntimeError: boom",
        "### 2026-10-03 · submit · skipped: market closed today",
    ]
