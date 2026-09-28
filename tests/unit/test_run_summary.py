from __future__ import annotations

from datetime import UTC, date, datetime

from trader.agent import MalformedProposal
from trader.db.repo import NewRun
from trader.models import (
    AccountState,
    Order,
    OrderStatus,
    RunMode,
    RunStatus,
    Side,
    Usage,
    Verdict,
    VerdictStatus,
)
from trader.run import OrderLine, format_summary

RUN = NewRun(
    run_date=date(2026, 9, 28),
    started_at=datetime(2026, 9, 28, 12, 31, tzinfo=UTC),
    mode=RunMode.DRY_RUN,
    paper=True,
    model="claude-sonnet-5",
    prompt_version="0123456789",
)
ACCOUNT = AccountState(equity=10_234.56, cash=8_012.3)
USAGE = Usage(
    input_tokens=12_000,
    output_tokens=900,
    cache_write_tokens=8_000,
    cache_read_tokens=40_000,
    cost_usd=0.0612,
)
TRIMMED_BUY = Verdict(
    symbol="URA",
    action=Side.BUY,
    status=VerdictStatus.TRIMMED,
    reasons=("position cap: $1,200.00 trimmed to $818.76 (8% of equity)",),
    opens_new_position=True,
    order=Order(
        symbol="URA", side=Side.BUY, qty=20, limit_price=40.4, stop_price=37.0, take_profit_price=46.0
    ),
)


def summary(**changes: object) -> list[str]:
    arguments: dict[str, object] = {
        "run": RUN,
        "status": RunStatus.COMPLETED,
        "account": ACCOUNT,
        "usage": USAGE,
        "turns": 4,
        "submitted": True,
        "market_view": "Uranium leads on 1m relative strength.",
        "verdicts": [TRIMMED_BUY],
        "malformed": [],
        "orders": [
            OrderLine(side=Side.BUY, symbol="URA", qty=20, status=OrderStatus.NOT_SUBMITTED, detail="dry run")
        ],
    }
    return format_summary(**(arguments | changes)).splitlines()  # type: ignore[arg-type]  # a test's overrides


def test_summary_is_one_screen_of_what_happened() -> None:
    assert summary() == [
        "2026-09-28 · dry_run (paper) · completed",
        "Equity $10,234.56 · cash $8,012.30 · prompt 0123456789",
        "Cost $0.0612 over 4 model turns: "
        "12,000 input, 900 output, 8,000 cache write and 40,000 cache read tokens",
        "Market view: Uranium leads on 1m relative strength.",
        "Verdicts:",
        "  buy URA: trimmed · 20 at limit $40.40, stop $37.00, take-profit $46.00 · "
        "position cap: $1,200.00 trimmed to $818.76 (8% of equity)",
        "Orders:",
        "  buy 20 URA: not_submitted (dry run)",
    ]


def test_summary_lists_malformed_proposals_and_says_when_there_is_nothing() -> None:
    lines = summary(
        verdicts=[],
        orders=[],
        malformed=[MalformedProposal(raw={}, error="action: 'hold' is not buy or sell")],
    )

    assert lines[4:] == [
        "Verdicts: none",
        "Malformed proposals: 1",
        "  action: 'hold' is not buy or sell",
        "Orders: none",
    ]


def test_summary_says_when_the_model_never_submitted() -> None:
    lines = summary(submitted=False, market_view=None, verdicts=[], orders=[])

    assert lines[3] == "The model never called submit_proposals, so there are no trades."
