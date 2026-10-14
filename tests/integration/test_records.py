from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from trader.db import repo
from trader.db.repo import NewRun
from trader.db.tables import (
    account_snapshots,
    cancelled_orders,
    malformed_proposals,
    metadata,
    orders,
    position_snapshots,
    proposals,
    tool_calls,
    verdicts,
)
from trader.models import (
    AccountState,
    CancelReason,
    Order,
    OrderStatus,
    Position,
    Proposal,
    RunMode,
    Side,
    Verdict,
    VerdictStatus,
)

STARTED = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)
NAN, INF = float("nan"), float("inf")
REPLACED = "\N{REPLACEMENT CHARACTER}"  # what a NUL character becomes

ENTRY = Order(
    symbol="XLE", side=Side.BUY, qty=52, limit_price=95.96, stop_price=87.4, take_profit_price=106.4
)
EXIT = Order(symbol="SMH", side=Side.SELL, qty=3)
POSITION_NUMBERS = ("qty", "avg_entry_price", "current_price", "market_value", "unrealized_plpc")


@pytest.fixture
def run_id(conn: Connection) -> UUID:
    run = NewRun(
        run_date=date(2026, 9, 28),
        started_at=STARTED,
        mode=RunMode.SUBMIT,
        paper=True,
        model="claude-sonnet-5",
        prompt_version=None,
    )
    run_id = repo.start_run(conn, run)
    assert run_id is not None
    return run_id


def record_decision(conn: Connection, run_id: UUID, order: Order, *, seq: int = 0) -> int:
    """An approved proposal whose order is `order`. Returns the proposal's ID."""
    buy = order.side == Side.BUY
    proposal = Proposal(
        symbol=order.symbol,
        action=order.side,
        thesis="t",
        invalidation="i",
        target_pct=5.0 if buy else None,
        stop_pct=8.0 if buy else None,
    )
    verdict = Verdict(
        symbol=order.symbol,
        action=order.side,
        status=VerdictStatus.APPROVED,
        reasons=() if buy else ("full exit",),
        opens_new_position=buy,
        order=order,
    )
    raw = {"symbol": order.symbol, "action": order.side.value, "thesis": "t", "invalidation": "i"}
    return repo.record_proposal(conn, run_id, seq=seq, proposal=proposal, raw=raw, verdict=verdict)


def test_account_snapshot_stores_the_account_and_each_position(conn: Connection, run_id: UUID) -> None:
    xle = Position(
        symbol="XLE",
        qty=12.0,
        avg_entry_price=90.1234,
        current_price=95.5,
        market_value=1146.0,
        unrealized_plpc=0.059672,
    )
    smh = Position(
        symbol="SMH",
        qty=3.5,
        avg_entry_price=250.0,
        current_price=240.0,
        market_value=840.0,
        unrealized_plpc=-0.04,
    )

    repo.record_account_snapshot(
        conn, run_id, AccountState(equity=100_123.45, cash=40_000.1, positions=(xle, smh)), taken_at=STARTED
    )

    snapshot = conn.execute(select(account_snapshots)).one()
    assert tuple(snapshot) == (run_id, Decimal("100123.45"), Decimal("40000.10"), STARTED)
    columns = [position_snapshots.c[name] for name in ("symbol", *POSITION_NUMBERS)]
    stored = conn.execute(select(*columns).order_by(position_snapshots.c.symbol)).all()
    assert [tuple(row) for row in stored] == [
        ("SMH", Decimal("3.5"), Decimal(250), Decimal(240), Decimal(840), Decimal("-0.04")),
        ("XLE", Decimal(12), Decimal("90.1234"), Decimal("95.5"), Decimal(1146), Decimal("0.059672")),
    ]


def test_numbers_that_are_not_finite_are_stored_as_null(conn: Connection, run_id: UUID) -> None:
    position = Position(
        symbol="XLE", qty=12.0, avg_entry_price=INF, current_price=NAN, market_value=-INF, unrealized_plpc=NAN
    )

    repo.record_account_snapshot(
        conn, run_id, AccountState(equity=NAN, cash=INF, positions=(position,)), taken_at=STARTED
    )

    snapshot = conn.execute(select(account_snapshots.c.equity, account_snapshots.c.cash)).one()
    assert tuple(snapshot) == (None, None)
    stored = conn.execute(select(*(position_snapshots.c[name] for name in POSITION_NUMBERS))).one()
    assert tuple(stored) == (Decimal(12), None, None, None, None)


def test_tool_call_keeps_the_start_of_its_result_and_json_postgres_accepts(
    conn: Connection, run_id: UUID
) -> None:
    tool_input = {
        "symbol": "XLE",
        "days": NAN,
        "range": [INF, -INF, 3],
        "odd\x00key": "a\x00b",
        "n": {"x": 1.5},
    }

    repo.record_tool_call(
        conn, run_id, seq=0, name="get_news", tool_input=tool_input, result="Untrusted\x00" + "y" * 2000
    )
    repo.record_tool_call(
        conn, run_id, seq=1, name="submit_proposals", tool_input={"proposals": []}, result=None
    )

    news, submit = conn.execute(select(tool_calls).order_by(tool_calls.c.seq)).all()
    assert news.input == {
        "symbol": "XLE",
        "days": "NaN",
        "range": ["Infinity", "-Infinity", 3],
        f"odd{REPLACED}key": f"a{REPLACED}b",
        "n": {"x": 1.5},
    }
    assert news.result_excerpt == f"Untrusted{REPLACED}" + "y" * 1490  # 1,500 characters
    assert (submit.name, submit.input, submit.result_excerpt) == ("submit_proposals", {"proposals": []}, None)


def test_proposals_are_stored_with_their_verdicts(conn: Connection, run_id: UUID) -> None:
    entry = Proposal(
        symbol="XLE",
        action=Side.BUY,
        thesis="Energy leads\x00",
        invalidation="XLE closes below $88",
        target_pct=10.0,
        stop_pct=8.0,
        take_profit_pct=12.0,
        confidence=0.7,
    )
    trimmed = Verdict(
        symbol="XLE",
        action="buy",
        status=VerdictStatus.TRIMMED,
        reasons=("position cap: $10,000.00 trimmed to $8,000.00 (8% of equity)",),
        opens_new_position=True,
        order=ENTRY,
    )
    exit_ = Proposal(symbol="SMH", action=Side.SELL, thesis="Chips rolled over", invalidation="SMH new high")
    full_exit = Verdict(
        symbol="SMH", action="sell", status=VerdictStatus.APPROVED, reasons=("full exit",), order=EXIT
    )
    blocked = Proposal(
        symbol="tqqq", action=Side.BUY, thesis="t", invalidation="i", target_pct=NAN, stop_pct=5.0
    )
    rejected = Verdict(
        symbol="TQQQ", action="buy", status=VerdictStatus.REJECTED, reasons=("blocklist: TQQQ is blocked",)
    )
    decisions: list[tuple[Proposal, dict[str, object], Verdict]] = [
        (entry, {"symbol": "XLE", "action": "buy", "thesis": "Energy leads\x00", "confidence": 0.7}, trimmed),
        (exit_, {"symbol": "SMH", "action": "sell"}, full_exit),
        (blocked, {"symbol": "tqqq", "action": "buy", "target_pct": NAN, "stop_pct": 5}, rejected),
    ]

    ids = [
        repo.record_proposal(conn, run_id, seq=seq, proposal=proposal, raw=raw, verdict=verdict)
        for seq, (proposal, raw, verdict) in enumerate(decisions)
    ]

    stored = conn.execute(
        select(proposals, verdicts).select_from(proposals.join(verdicts)).order_by(proposals.c.seq)
    ).all()
    assert [row.id for row in stored] == ids
    buy, sell, refused = stored
    assert (buy.symbol, buy.action, buy.target_pct, buy.stop_pct, buy.take_profit_pct, buy.confidence) == (
        "XLE",
        "buy",
        Decimal(10),
        Decimal(8),
        Decimal(12),
        Decimal("0.7"),
    )
    assert (buy.thesis, buy.invalidation, buy.raw["thesis"]) == (
        f"Energy leads{REPLACED}",
        "XLE closes below $88",
        f"Energy leads{REPLACED}",
    )
    assert (buy.status, buy.reasons, buy.opens_new_position) == ("trimmed", list(trimmed.reasons), True)
    assert (buy.qty, buy.limit_price, buy.stop_price, buy.take_profit_price) == (
        52,
        Decimal("95.96"),
        Decimal("87.40"),
        Decimal("106.40"),
    )
    # A sell is a full exit at market: a quantity and no prices.
    assert (sell.action, sell.status, sell.reasons, sell.opens_new_position) == (
        "sell",
        "approved",
        ["full exit"],
        False,
    )
    assert (sell.qty, sell.limit_price, sell.stop_price, sell.take_profit_price) == (3, None, None, None)
    # A rejection has no order. The model's NaN is NULL in its column and a string in raw.
    assert (refused.symbol, refused.status, refused.qty, refused.limit_price) == (
        "tqqq",
        "rejected",
        None,
        None,
    )
    assert (refused.target_pct, refused.raw["target_pct"]) == (None, "NaN")


def test_malformed_proposals_keep_what_the_model_sent(conn: Connection, run_id: UUID) -> None:
    repo.record_malformed_proposal(
        conn,
        run_id,
        raw={"symbol": "XLE", "action": "short", "target_pct": INF},
        error="action: 'short' is not buy or sell",
    )
    repo.record_malformed_proposal(conn, run_id, raw="buy XLE\x00", error="a proposal must be an object")

    stored = conn.execute(
        select(malformed_proposals.c.raw, malformed_proposals.c.error).order_by(malformed_proposals.c.id)
    ).all()
    assert [tuple(row) for row in stored] == [
        (
            {"symbol": "XLE", "action": "short", "target_pct": "Infinity"},
            "action: 'short' is not buy or sell",
        ),
        (f"buy XLE{REPLACED}", "a proposal must be an object"),
    ]


def test_orders_record_what_became_of_them(conn: Connection, run_id: UUID) -> None:
    entry_id = record_decision(conn, run_id, ENTRY, seq=0)
    exit_id = record_decision(conn, run_id, EXIT, seq=1)
    submitted = repo.record_order(
        conn,
        run_id,
        proposal_id=entry_id,
        client_order_id="llmt-2026-09-28-XLE-buy",
        order=ENTRY,
        opens_new_position=True,
        status=OrderStatus.SUBMITTED,
        broker_order_id="61e7b016",
        broker_status="accepted",
    )
    held_back = repo.record_order(
        conn,
        run_id,
        proposal_id=entry_id,
        client_order_id="llmt-2026-09-28-XLE-buy",
        order=ENTRY,
        opens_new_position=True,
        status=OrderStatus.NOT_SUBMITTED,
        not_submitted_reason="trading_enabled is false",
    )
    failed = repo.record_order(
        conn,
        run_id,
        proposal_id=exit_id,
        client_order_id="llmt-2026-09-28-SMH-sell",
        order=EXIT,
        opens_new_position=False,
        status=OrderStatus.ERROR,
        error="APIError: insufficient qty\x00",
    )

    stored = {row.id: row for row in conn.execute(select(orders)).all()}
    assert set(stored) == {submitted, held_back, failed}
    entry = stored[submitted]
    assert (entry.run_id, entry.proposal_id, entry.client_order_id, entry.symbol, entry.side, entry.qty) == (
        run_id,
        entry_id,
        "llmt-2026-09-28-XLE-buy",
        "XLE",
        "buy",
        52,
    )
    assert (entry.limit_price, entry.stop_price, entry.take_profit_price) == (
        Decimal("95.96"),
        Decimal("87.40"),
        Decimal("106.40"),
    )
    assert (entry.status, entry.broker_order_id, entry.broker_status, entry.opens_new_position) == (
        "submitted",
        "61e7b016",
        "accepted",
        True,
    )
    assert entry.created_at.tzinfo is not None
    assert (stored[held_back].status, stored[held_back].not_submitted_reason) == (
        "not_submitted",
        "trading_enabled is false",
    )
    exit_ = stored[failed]
    assert (exit_.side, exit_.limit_price, exit_.stop_price, exit_.take_profit_price) == (
        "sell",
        None,
        None,
        None,
    )
    assert (exit_.status, exit_.error) == ("error", f"APIError: insufficient qty{REPLACED}")


def test_a_client_order_id_can_be_submitted_twice(conn: Connection, run_id: UUID) -> None:
    # HANDOFF §10: the row is written after the broker call, so refusing a second one would only lose the
    # record of an order that was placed, as when `make offline` runs twice in a day.
    entry_id = record_decision(conn, run_id, ENTRY)
    for broker_order_id in ("fake-1", "fake-2"):
        repo.record_order(
            conn,
            run_id,
            proposal_id=entry_id,
            client_order_id="llmt-2026-09-28-XLE-buy",
            order=ENTRY,
            opens_new_position=True,
            status=OrderStatus.SUBMITTED,
            broker_order_id=broker_order_id,
        )

    assert conn.execute(select(func.count()).select_from(orders)).scalar_one() == 2


def test_entry_order_finds_only_the_submitted_buy_with_that_broker_id(conn: Connection, run_id: UUID) -> None:
    """A run re-places a partially filled entry's stop at this price, against this proposal (HANDOFF §8)."""
    entry_id = record_decision(conn, run_id, ENTRY)
    exit_id = record_decision(conn, run_id, EXIT, seq=1)
    for proposal_id, order, status, broker_order_id in [
        (entry_id, ENTRY, OrderStatus.SUBMITTED, "b-entry"),
        (exit_id, EXIT, OrderStatus.SUBMITTED, "b-exit"),
        (entry_id, ENTRY, OrderStatus.ERROR, "b-refused"),  # the broker never had it
    ]:
        repo.record_order(
            conn,
            run_id,
            proposal_id=proposal_id,
            client_order_id=f"llmt-2026-09-28-{order.symbol}-{order.side.value}",
            order=order,
            opens_new_position=order.side == Side.BUY,
            status=status,
            broker_order_id=broker_order_id,
        )

    assert repo.entry_order(conn, "b-entry") == repo.EntryOrder(proposal_id=entry_id, stop_price=87.4)
    assert repo.entry_order(conn, "b-exit") is None  # a sell isn't an entry
    assert repo.entry_order(conn, "b-refused") is None
    assert repo.entry_order(conn, "b-unknown") is None


def test_cancelled_orders_may_lack_a_symbol(conn: Connection, run_id: UUID) -> None:
    repo.record_cancelled_order(
        conn, run_id, broker_order_id="b-1", symbol=None, reason=CancelReason.STALE_ENTRY
    )
    repo.record_cancelled_order(
        conn, run_id, broker_order_id="b-2", symbol="SMH", reason=CancelReason.EXIT_LEGS
    )

    columns = (cancelled_orders.c.broker_order_id, cancelled_orders.c.symbol, cancelled_orders.c.reason)
    stored = conn.execute(select(*columns).order_by(cancelled_orders.c.id)).all()
    assert [tuple(row) for row in stored] == [("b-1", None, "stale_entry"), ("b-2", "SMH", "exit_legs")]


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("runs", "mode", "live"),
        ("runs", "status", "done"),
        ("proposals", "action", "short"),
        ("verdicts", "status", "maybe"),
        ("orders", "side", "short"),
        ("orders", "status", "pending"),
        ("cancelled_orders", "reason", "other"),
    ],
)
def test_check_constraints_refuse_unknown_values(
    conn: Connection, run_id: UUID, table: str, column: str, value: str
) -> None:
    entry_id = record_decision(conn, run_id, ENTRY)
    repo.record_order(
        conn,
        run_id,
        proposal_id=entry_id,
        client_order_id="llmt-2026-09-28-XLE-buy",
        order=ENTRY,
        opens_new_position=True,
        status=OrderStatus.SUBMITTED,
    )
    repo.record_cancelled_order(
        conn, run_id, broker_order_id="b-1", symbol="XLE", reason=CancelReason.EXIT_LEGS
    )

    with pytest.raises(IntegrityError, match=f'violates check constraint "ck_{table}_{column}"'):
        conn.execute(update(metadata.tables[table]).values({column: value}))


def test_json_that_skips_repo_is_refused_before_it_reaches_postgres(conn: Connection, run_id: UUID) -> None:
    # repo.py makes JSON storable; the engine's strict serializer is the backstop if something skips it.
    with pytest.raises(ValueError, match="not JSON compliant"):
        conn.execute(insert(malformed_proposals).values(run_id=run_id, raw={"target_pct": NAN}, error="e"))
