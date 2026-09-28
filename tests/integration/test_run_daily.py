"""run_daily end to end, against the test database, FakeBroker and ScriptedClient (HANDOFF §9 and §14).

run_daily commits its own transactions, so each test hands it the `engine` fixture and reads the results
through `conn`, whose fixture empties every table before the test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from anthropic.types import Message
from sqlalchemy import Connection, Engine, Table, func, select, text

from trader.brokers.base import BrokerError, SubmittedOrder
from trader.brokers.fake import FakeBroker, Holding, OpenOrder
from trader.db import repo
from trader.db.repo import NewRun, SchemaVersionError
from trader.db.tables import (
    SCHEMA_HEAD,
    account_snapshots,
    cancelled_orders,
    malformed_proposals,
    orders,
    position_snapshots,
    prompt_versions,
    proposals,
    runs,
    tool_calls,
    verdicts,
)
from trader.models import AccountState, Bar, Order, Policy, RunMode, RunStatus, Side, StopPolicy, Usage
from trader.run import ALREADY_RAN, MARKET_CLOSED, ForceRefusedError, LiveMoneyError, RunResult, run_daily
from trader.scripted import SCRIPTED_USAGE, ScriptedClient, ScriptExhausted, reply, submit, tool_use
from trader.scripted import text as words
from trader.settings import Config, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
MONDAY = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York
SATURDAY = datetime(2026, 10, 3, 12, 31, tzinfo=UTC)
RUN_DATE = date(2026, 9, 28)

# HANDOFF Appendix C's numbers, fixed here so tuning config/policy.yaml never changes these tests.
POLICY = Policy(
    trading_enabled=True,
    allow_live_money=False,
    max_position_pct=8.0,
    max_open_positions=6,
    max_new_positions_per_week=4,
    min_cash_buffer_pct=5.0,
    min_price=5.0,
    min_avg_dollar_volume=5_000_000.0,
    max_pct_of_adv=1.0,
    entry_limit_buffer_pct=1.0,
    stop=StopPolicy(required=True, min_pct=3.0, max_pct=15.0),
    drawdown_freeze_pct=15.0,
    drawdown_peak_since=None,
    blocked_symbols=frozenset({"TQQQ", "SQQQ"}),
)
CONFIG = replace(load_config(REPO_ROOT), policy=POLICY)

SMH_LEGS = (
    OpenOrder(broker_order_id="smh-stop", symbol="SMH", side=Side.SELL),
    OpenOrder(broker_order_id="smh-take-profit", symbol="SMH", side=Side.SELL),
)
STALE_ENTRY = OpenOrder(broker_order_id="igv-entry", symbol="IGV", side=Side.BUY)


def proposal(symbol: str, action: str = "buy", **changes: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": symbol,
        "action": action,
        "thesis": f"A catalyst for {symbol}.",
        "invalidation": f"{symbol} closes below its 20-day low.",
        "confidence": 0.6,
    }
    if action == "buy":
        item |= {"target_pct": 5, "stop_pct": 8}
    return item | changes


def research_then_submit(*items: dict[str, object]) -> list[Message]:
    return [
        reply(
            words("Checking energy."),
            tool_use("get_price_history", {"symbol": "XLE"}),
            tool_use("get_news", {"symbol": "XLE"}),
        ),
        reply(submit("Energy leads; semis are rolling over.", list(items))),
    ]


def fake_broker(now: datetime = MONDAY, **changes: Any) -> FakeBroker:
    """A paper account holding 5 SMH with its two protective legs, and yesterday's unfilled IGV entry."""
    settings: dict[str, Any] = {
        "holdings": [Holding(symbol="SMH", qty=5, avg_entry_price=100.0)],
        "open_orders": [*SMH_LEGS, STALE_ENTRY],
    }
    return FakeBroker(now=now, **(settings | changes))


def run(
    engine: Engine,
    responses: Sequence[Message],
    *,
    broker: FakeBroker | None = None,
    mode: RunMode = RunMode.SUBMIT,
    config: Config = CONFIG,
    now: datetime = MONDAY,
    force: bool = False,
) -> RunResult:
    return run_daily(
        mode=mode,
        config=config,
        engine=engine,
        broker=fake_broker(now) if broker is None else broker,
        client=ScriptedClient(responses),
        clock=lambda: now,
        force=force,
    )


def count(conn: Connection, table: Table) -> int:
    total: object = conn.execute(select(func.count()).select_from(table)).scalar_one()
    assert isinstance(total, int)
    return total


def run_row(conn: Connection, result: RunResult) -> Any:  # a Row, read by column name
    return conn.execute(select(runs).where(runs.c.id == result.run_id)).one()


def order_rows(conn: Connection) -> list[tuple[Any, ...]]:
    query = select(
        orders.c.client_order_id, orders.c.status, orders.c.broker_order_id, orders.c.not_submitted_reason
    ).order_by(orders.c.id)
    return [tuple(row) for row in conn.execute(query)]


def past_submit_run(conn: Connection, *, started_at: datetime, status: RunStatus = RunStatus.RUNNING) -> Any:
    """A submit run for RUN_DATE left in `status`, committed so run_daily sees it."""
    new_run = NewRun(
        run_date=RUN_DATE,
        started_at=started_at,
        mode=RunMode.SUBMIT,
        paper=True,
        model="m",
        prompt_version=None,
    )
    run_id = repo.start_run(conn, new_run)
    assert run_id is not None
    if status is RunStatus.COMPLETED:
        repo.finish_run(
            conn,
            run_id,
            finished_at=started_at,
            briefing="b",
            market_view=None,
            agent_submitted=True,
            agent_turns=1,
            usage=Usage(),
        )
    conn.commit()
    return run_id


class RecordingBroker(FakeBroker):
    """Remembers each bars request."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bar_requests: list[tuple[list[str], int]] = []

    def get_daily_bars(self, symbols: Sequence[str], sessions: int) -> dict[str, list[Bar]]:
        self.bar_requests.append((list(symbols), sessions))
        return super().get_daily_bars(symbols, sessions)


class FailingBroker(FakeBroker):
    """Fails to submit one symbol's order, with the given exception."""

    def __init__(self, symbol: str, error: Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.symbol, self.error = symbol, error

    def submit(self, order: Order, client_order_id: str) -> SubmittedOrder:
        if order.symbol == self.symbol:
            self.calls.append("submit")
            raise self.error
        return super().submit(order, client_order_id)


# ---- A whole run -------------------------------------------------------------------------------------------


def test_submit_run_records_every_step(engine: Engine, conn: Connection) -> None:
    broker = fake_broker()

    result = run(
        engine,
        research_then_submit(
            proposal("URA"), proposal("SMH", "sell"), proposal("TQQQ"), {"symbol": "XLE", "action": "hold"}
        ),
        broker=broker,
    )

    assert result.status is RunStatus.COMPLETED
    row = run_row(conn, result)
    assert (row.status, row.mode, row.paper, row.run_date) == ("completed", "submit", True, RUN_DATE)
    assert (row.agent_submitted, row.agent_turns, row.prompt_version) == (True, 2, CONFIG.prompt_version)
    assert row.briefing == result.briefing and row.briefing.startswith("# Daily briefing: 2026-09-28")
    assert row.market_view == "Energy leads; semis are rolling over."
    assert (row.input_tokens, row.output_tokens) == (
        2 * SCRIPTED_USAGE.input_tokens,
        2 * SCRIPTED_USAGE.output_tokens,
    )
    assert row.cost_usd > 0
    assert row.finished_at == MONDAY
    assert (
        count(conn, prompt_versions) == count(conn, account_snapshots) == count(conn, position_snapshots) == 1
    )
    names: Sequence[object] = (
        conn.execute(select(tool_calls.c.name).order_by(tool_calls.c.seq)).scalars().all()
    )
    assert names == ["get_price_history", "get_news", "submit_proposals"]
    decided = conn.execute(
        select(proposals.c.seq, proposals.c.symbol, verdicts.c.status)
        .join(verdicts)
        .order_by(proposals.c.seq)
    ).all()
    assert [tuple(row) for row in decided] == [
        (0, "URA", "approved"),
        (1, "SMH", "approved"),
        (2, "TQQQ", "rejected"),
    ]
    assert conn.execute(select(malformed_proposals.c.error)).scalars().all() == [
        "action: 'hold' is not buy or sell"
    ]
    # Exits go first, then entries.
    assert order_rows(conn) == [
        ("llmt-2026-09-28-SMH-sell", "submitted", "fake-1", None),
        ("llmt-2026-09-28-URA-buy", "submitted", "fake-2", None),
    ]
    assert count(conn, cancelled_orders) == 3  # the stale IGV entry and SMH's two legs
    summary = result.summary.splitlines()
    assert summary[0] == "2026-09-28 · submit (paper) · completed"
    assert "  sell SMH: approved · 5 at market · full exit" in summary
    assert "  buy TQQQ: rejected · blocklist: TQQQ is blocked" in summary
    assert "  sell 5 SMH: submitted (fake-1, accepted)" in summary


def test_stale_entries_are_cancelled_before_the_account_is_read(engine: Engine, conn: Connection) -> None:
    broker = fake_broker()

    run(engine, research_then_submit(), broker=broker)

    assert broker.calls.index("cancel_open_buy_orders") < broker.calls.index("get_account")
    stale = conn.execute(
        select(cancelled_orders.c.broker_order_id, cancelled_orders.c.symbol, cancelled_orders.c.reason)
    )
    assert [tuple(row) for row in stale] == [("igv-entry", "IGV", "stale_entry")]
    assert broker.open_orders == list(SMH_LEGS)  # the legs of a held position stay


def test_an_exit_cancels_its_legs_before_selling(engine: Engine, conn: Connection) -> None:
    broker = fake_broker(open_orders=list(SMH_LEGS))

    run(engine, research_then_submit(proposal("SMH", "sell")), broker=broker)

    assert broker.calls[-2:] == ["cancel_open_orders", "submit"]
    assert broker.submitted == [(Order(symbol="SMH", side=Side.SELL, qty=5), "llmt-2026-09-28-SMH-sell")]
    legs = conn.execute(
        select(
            cancelled_orders.c.broker_order_id, cancelled_orders.c.symbol, cancelled_orders.c.reason
        ).order_by(cancelled_orders.c.id)
    )
    assert [tuple(row) for row in legs] == [
        ("smh-stop", "SMH", "exit_legs"),
        ("smh-take-profit", "SMH", "exit_legs"),
    ]


def test_the_model_never_submitting_means_no_trades(engine: Engine, conn: Connection) -> None:
    result = run(engine, [reply(words("Hmm.")), reply(words("I'd rather not decide."))])

    row = run_row(conn, result)
    assert (row.status, row.agent_submitted, row.agent_turns, row.market_view) == (
        "completed",
        False,
        2,
        None,
    )
    assert count(conn, proposals) == count(conn, orders) == 0
    assert "The model never called submit_proposals, so there are no trades." in result.summary


def test_proposed_buys_the_run_hasnt_seen_get_25_sessions_of_bars(engine: Engine, conn: Connection) -> None:
    broker = RecordingBroker(now=MONDAY)

    run(engine, [reply(submit("Uranium miners.", [proposal("ccj"), proposal("XLE")]))], broker=broker)

    assert broker.bar_requests[-1] == (["CCJ"], 25)  # XLE was in the briefing already
    statuses: Sequence[object] = (
        conn.execute(select(verdicts.c.status).join(proposals).order_by(proposals.c.seq)).scalars().all()
    )
    assert statuses == ["approved", "approved"]


# ---- Modes and switches ------------------------------------------------------------------------------------


def test_a_dry_run_submits_nothing(engine: Engine, conn: Connection) -> None:
    broker = fake_broker()

    run(
        engine,
        research_then_submit(proposal("URA"), proposal("SMH", "sell")),
        broker=broker,
        mode=RunMode.DRY_RUN,
    )

    assert not {"cancel_open_buy_orders", "cancel_open_orders", "submit"} & set(broker.calls)
    assert broker.submitted == [] and broker.cancelled == []
    assert order_rows(conn) == [
        ("llmt-2026-09-28-SMH-sell", "not_submitted", None, "dry run"),
        ("llmt-2026-09-28-URA-buy", "not_submitted", None, "dry run"),
    ]
    assert count(conn, cancelled_orders) == 0


@pytest.mark.parametrize("mode", [RunMode.SUBMIT, RunMode.DRY_RUN, RunMode.OFFLINE])
def test_the_kill_switch_holds_every_order_back(engine: Engine, conn: Connection, mode: RunMode) -> None:
    broker = fake_broker()
    config = replace(CONFIG, policy=replace(POLICY, trading_enabled=False))

    result = run(
        engine,
        research_then_submit(proposal("URA"), proposal("SMH", "sell")),
        broker=broker,
        mode=mode,
        config=config,
    )

    assert not {"cancel_open_buy_orders", "cancel_open_orders", "submit"} & set(
        broker.calls
    )  # no broker writes
    assert {row[1:] for row in order_rows(conn)} == {("not_submitted", None, "trading_enabled is false")}
    assert count(conn, cancelled_orders) == 0
    assert run_row(conn, result).status == "completed"  # it still researched, evaluated and recorded


def test_an_offline_run_places_its_orders_with_the_fake(engine: Engine, conn: Connection) -> None:
    broker = fake_broker()

    result = run(engine, research_then_submit(proposal("URA")), broker=broker, mode=RunMode.OFFLINE)

    assert run_row(conn, result).mode == "offline"
    assert [client_order_id for _, client_order_id in broker.submitted] == ["llmt-2026-09-28-URA-buy"]
    assert order_rows(conn) == [("llmt-2026-09-28-URA-buy", "submitted", "fake-1", None)]


def test_offline_runs_need_the_fake_broker(engine: Engine, conn: Connection) -> None:
    with pytest.raises(ValueError, match="offline runs use FakeBroker"):
        run_daily(
            mode=RunMode.OFFLINE,
            config=CONFIG,
            engine=engine,
            broker=cast(Any, object()),  # anything that isn't FakeBroker, such as the Alpaca adapter
            client=ScriptedClient([]),
            clock=lambda: MONDAY,
        )

    assert count(conn, runs) == 0


# ---- Guards ------------------------------------------------------------------------------------------------


def test_a_weekend_is_skipped(engine: Engine, conn: Connection) -> None:
    broker = fake_broker(SATURDAY)

    result = run(engine, [], broker=broker, now=SATURDAY)

    assert (result.status, result.summary) == (
        RunStatus.SKIPPED,
        "2026-10-03 · submit (paper) · skipped: market closed today",
    )
    row = run_row(conn, result)
    assert (row.status, row.skip_reason, row.run_date) == ("skipped", MARKET_CLOSED, date(2026, 10, 3))
    assert broker.calls == ["is_trading_day"]
    assert count(conn, account_snapshots) == 0


def test_only_one_submit_run_a_day(engine: Engine, conn: Connection) -> None:
    first = run(engine, research_then_submit())
    second_broker = fake_broker()

    second = run(engine, research_then_submit(), broker=second_broker)

    assert (first.status, second.status) == (RunStatus.COMPLETED, RunStatus.SKIPPED)
    assert run_row(conn, second).skip_reason == ALREADY_RAN
    assert second_broker.calls == ["is_trading_day"]  # no account call, no orders
    assert run(engine, research_then_submit(), mode=RunMode.DRY_RUN).status is RunStatus.COMPLETED


def test_a_leftover_running_submit_run_blocks_the_day_without_force(engine: Engine, conn: Connection) -> None:
    stale = past_submit_run(conn, started_at=MONDAY - timedelta(hours=2))

    result = run(engine, research_then_submit())

    assert (result.status, run_row(conn, result).skip_reason) == (RunStatus.SKIPPED, ALREADY_RAN)
    assert conn.execute(select(runs.c.status).where(runs.c.id == stale)).scalar_one() == "running"


def test_force_abandons_a_stale_running_row(engine: Engine, conn: Connection) -> None:
    stale = past_submit_run(conn, started_at=MONDAY - timedelta(minutes=30))

    result = run(engine, research_then_submit(), force=True)

    assert result.status is RunStatus.COMPLETED
    assert conn.execute(select(runs.c.status).where(runs.c.id == stale)).scalar_one() == "abandoned"


def test_force_refuses_to_abandon_a_run_that_may_still_be_going(engine: Engine, conn: Connection) -> None:
    young = past_submit_run(conn, started_at=MONDAY - timedelta(minutes=5))
    broker = fake_broker()

    with pytest.raises(ForceRefusedError, match="started 5 minutes ago and may still be running"):
        run(engine, research_then_submit(), broker=broker, force=True)

    assert conn.execute(select(runs.c.status).where(runs.c.id == young)).scalar_one() == "running"
    assert count(conn, runs) == 1
    assert "get_account" not in broker.calls


def test_force_never_allows_a_second_completed_submit_run(engine: Engine, conn: Connection) -> None:
    past_submit_run(conn, started_at=MONDAY - timedelta(hours=1), status=RunStatus.COMPLETED)

    result = run(engine, research_then_submit(), force=True)

    assert (result.status, run_row(conn, result).skip_reason) == (RunStatus.SKIPPED, ALREADY_RAN)


def test_the_live_money_guard_fires_before_any_account_call(engine: Engine, conn: Connection) -> None:
    broker = fake_broker(is_paper=False)

    with pytest.raises(LiveMoneyError, match="allow_live_money: false"):
        run(engine, research_then_submit(), broker=broker)

    assert broker.calls == []
    assert count(conn, runs) == count(conn, prompt_versions) == 0


def test_live_money_runs_when_the_policy_allows_it(engine: Engine, conn: Connection) -> None:
    config = replace(CONFIG, policy=replace(POLICY, allow_live_money=True))

    result = run(engine, research_then_submit(), broker=fake_broker(is_paper=False), config=config)

    assert run_row(conn, result).paper is False
    assert result.summary.startswith("2026-09-28 · submit (LIVE) · completed")


def test_the_schema_guard_fires_first(engine: Engine, conn: Connection) -> None:
    broker = fake_broker()
    conn.execute(text("UPDATE alembic_version SET version_num = '0ld0ld0ld0ld'"))
    conn.commit()
    try:
        with pytest.raises(SchemaVersionError, match="has revision 0ld0ld0ld0ld"):
            run(engine, research_then_submit(), broker=broker)
    finally:
        conn.execute(text("UPDATE alembic_version SET version_num = :head"), {"head": SCHEMA_HEAD})
        conn.commit()

    assert broker.calls == []
    assert count(conn, runs) == 0


# ---- Risk context ------------------------------------------------------------------------------------------


def test_the_risk_context_comes_from_earlier_runs(engine: Engine, conn: Connection) -> None:
    friday = datetime(2026, 9, 25, 12, 31, tzinfo=UTC)
    earlier = NewRun(
        run_date=date(2026, 9, 25),
        started_at=friday,
        mode=RunMode.SUBMIT,
        paper=True,
        model="m",
        prompt_version=None,
    )
    run_id = repo.start_run(conn, earlier)
    assert run_id is not None
    repo.record_account_snapshot(conn, run_id, AccountState(equity=50_000.0, cash=50_000.0), taken_at=friday)
    conn.commit()

    submitted = run(engine, research_then_submit(proposal("URA")))
    offline = run(engine, research_then_submit(proposal("URA")), mode=RunMode.OFFLINE)

    assert "FREEZE ACTIVE" in (submitted.briefing or "")  # equity is far below Friday's $50,000
    assert "buy URA: rejected · drawdown freeze" in submitted.summary
    assert "buy URA: approved" in offline.summary  # offline runs ignore the account's history


# ---- Failures and the persistence order --------------------------------------------------------------------


def test_a_failure_marks_the_run_failed_and_keeps_its_usage(engine: Engine, conn: Connection) -> None:
    with pytest.raises(ScriptExhausted):
        run(engine, [reply(tool_use("get_news", {"symbol": "XLE"}))])  # the model call after it fails

    row = conn.execute(select(runs)).one()
    assert row.status == "failed"
    assert row.error.startswith("ScriptExhausted: no scripted response left for model call 2")
    assert (row.input_tokens, row.output_tokens) == (
        SCRIPTED_USAGE.input_tokens,
        SCRIPTED_USAGE.output_tokens,
    )
    assert row.finished_at == MONDAY
    assert count(conn, account_snapshots) == 1  # committed right after the account read


def test_a_crash_between_orders_keeps_the_record_of_what_was_sent(engine: Engine, conn: Connection) -> None:
    broker = FailingBroker(
        "URA",
        RuntimeError("connection reset"),
        now=MONDAY,
        holdings=[Holding(symbol="SMH", qty=5, avg_entry_price=100.0)],
        open_orders=list(SMH_LEGS),
    )

    with pytest.raises(RuntimeError, match="connection reset"):
        run(engine, research_then_submit(proposal("SMH", "sell"), proposal("URA")), broker=broker)

    # The exit went first and its row was committed at once; the evaluation before it was too.
    assert order_rows(conn) == [("llmt-2026-09-28-SMH-sell", "submitted", "fake-1", None)]
    assert count(conn, cancelled_orders) == 2
    assert count(conn, proposals) == count(conn, verdicts) == 2
    assert count(conn, tool_calls) == 3
    row = conn.execute(select(runs)).one()
    assert (row.status, row.error) == ("failed", "RuntimeError: connection reset")
    assert row.input_tokens == 2 * SCRIPTED_USAGE.input_tokens


def test_a_broker_error_on_one_order_is_recorded_and_the_rest_go(engine: Engine, conn: Connection) -> None:
    broker = FailingBroker("URA", BrokerError("insufficient buying power"), now=MONDAY)

    result = run(engine, research_then_submit(proposal("URA"), proposal("XLE")), broker=broker)

    assert result.status is RunStatus.COMPLETED
    failed = conn.execute(select(orders.c.status, orders.c.error).where(orders.c.symbol == "URA")).one()
    assert tuple(failed) == ("error", "BrokerError: insufficient buying power")
    assert order_rows(conn)[1] == ("llmt-2026-09-28-XLE-buy", "submitted", "fake-1", None)
    assert ": error (BrokerError: insufficient buying power)" in result.summary
