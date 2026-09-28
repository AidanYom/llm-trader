"""The daily run (HANDOFF §3 and §9): guards, briefing, agent, risk engine, orders and persistence.

This is the only module that sends orders (CLAUDE.md invariant 2). It sends them through a Broker, and only
the orders the risk engine's verdicts hold. It commits in HANDOFF §9's order, so a crash never loses the
record of a broker write:

1. The run's row, once the guards pass. Then stale-entry cancels right after the cancel call, and the
   account snapshot right after the account read.
2. Tool calls, proposals with their verdicts, and malformed proposals, after evaluation.
3. Each order, and an exit's cancelled legs, right after its broker call.
4. The summary fields and the `completed` status, last.

Any exception marks the run failed, with its error and the API usage so far, and is re-raised.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, Engine

from trader.agent import AgentResult, MalformedProposal, ModelClient, UsageMeter, run_agent
from trader.briefing import build_briefing, symbol_stats
from trader.brokers.base import Broker, BrokerError, CancelledOrder
from trader.brokers.fake import FakeBroker
from trader.db import repo
from trader.db.repo import NewRun
from trader.models import (
    AccountState,
    Bar,
    CancelReason,
    Order,
    OrderStatus,
    RiskContext,
    RunMode,
    RunStatus,
    Side,
    SymbolStats,
    Usage,
    Verdict,
    finite_float,
    new_york_date,
    normalize_symbol,
)
from trader.risk import evaluate
from trader.settings import Config

log = logging.getLogger(__name__)

MARKET_CLOSED = "market closed today"
ALREADY_RAN = "already ran in submit mode today"
DRY_RUN = "dry run"
KILL_SWITCH = "trading_enabled is false"
# --force leaves a younger running row alone: that run may still be going. Lambda can't run for more than
# 15 minutes, so an older row is certainly dead.
FORCE_MIN_AGE = timedelta(minutes=20)
BRIEFING_SESSIONS = 70  # HANDOFF §6
BUY_SESSIONS = 25  # HANDOFF §3 step 7: for proposed buys the run hasn't fetched yet
MARKET_NEWS_LIMIT = 50  # HANDOFF §6
POSITION_NEWS_LIMIT = 30


class LiveMoneyError(RuntimeError):
    """The account is live, and policy.yaml doesn't allow live money (CLAUDE.md invariant 5)."""


class ForceRefusedError(RuntimeError):
    """--force was asked to abandon a submit run that may still be going."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderLine:
    """An order as the run recorded it, for the summary."""

    side: Side
    symbol: str
    qty: int
    status: OrderStatus
    detail: str  # the broker's ID and status, or why the order wasn't sent


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    run_id: UUID
    status: RunStatus  # completed or skipped; a failed run raises instead
    summary: str
    briefing: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def run_daily(
    *,
    mode: RunMode,
    config: Config,
    engine: Engine,
    broker: Broker,
    client: ModelClient,
    clock: Callable[[], datetime] = utc_now,
    force: bool = False,
) -> RunResult:
    """One pre-market run (HANDOFF §3).

    `clock` returns the current time; tests pin it. The run date is the New York date of its first reading.
    `force` only applies to submit runs: it abandons the day's stale running submit run (HANDOFF §9).
    """
    started_at = clock()
    run_date = new_york_date(started_at)
    if mode is RunMode.OFFLINE and not isinstance(broker, FakeBroker):
        raise ValueError("offline runs use FakeBroker, so they can never reach a real broker (invariant 4)")

    # The schema and live-money guards come before the run's row exists: they raise, writing nothing.
    with engine.connect() as conn:
        repo.check_schema(conn)
    paper = broker.is_paper
    if not paper and not config.policy.allow_live_money:
        raise LiveMoneyError("the broker account is live, and policy.yaml has allow_live_money: false")

    run = NewRun(
        run_date=run_date,
        started_at=started_at,
        mode=mode,
        paper=paper,
        model=config.strategy.model,
        prompt_version=config.prompt_version,
    )
    trading_day = broker.is_trading_day(run_date)
    with engine.begin() as conn:
        repo.save_prompt_version(conn, version=config.prompt_version, system_prompt=config.system_prompt)
        if not trading_day:
            return _skipped(repo.record_skipped_run(conn, run, reason=MARKET_CLOSED), run, MARKET_CLOSED)
        if force and mode is RunMode.SUBMIT:
            _abandon_stale_run(conn, run)
        run_id = repo.start_run(conn, run)
        if run_id is None:  # the one-submit-run-per-day index refused it
            return _skipped(repo.record_skipped_run(conn, run, reason=ALREADY_RAN), run, ALREADY_RAN)

    log.info(
        "run started", extra={"run_id": run_id, "run_date": run_date, "mode": mode.value, "paper": paper}
    )
    meter = UsageMeter(config.strategy.price)
    try:
        return _Run(run_id, run, config, engine, broker, client, clock, meter).execute()
    except BaseException as exc:  # Ctrl-C too: a run left `running` would block the day's submit run
        _record_failure(engine, run_id, clock, exc, meter.usage)
        raise


class _Run:
    """A run's work once its row exists, step by step."""

    def __init__(
        self,
        run_id: UUID,
        run: NewRun,
        config: Config,
        engine: Engine,
        broker: Broker,
        client: ModelClient,
        clock: Callable[[], datetime],
        meter: UsageMeter,
    ) -> None:
        self.run_id = run_id
        self.run = run
        self.config = config
        self.policy = config.policy
        self.strategy = config.strategy
        self.engine = engine
        self.broker = broker
        self.client = client
        self.clock = clock
        self.meter = meter
        # Offline runs take the submit path against FakeBroker. The kill switch means no broker writes at all.
        self.writes_to_broker = run.mode in (RunMode.SUBMIT, RunMode.OFFLINE) and self.policy.trading_enabled
        self.unprotected: list[CancelledOrder] = []  # partially filled entries cancelled with their stops
        strategy = self.strategy
        self.universe = list(
            dict.fromkeys((strategy.benchmark, *strategy.sector_etfs, *strategy.industry_etfs))
        )

    def execute(self) -> RunResult:
        self._cancel_stale_entries()
        account = self._read_account()
        ctx = self._risk_context()
        briefing, bars = self._briefing(account, ctx)
        agent = run_agent(
            self.client,
            strategy=self.strategy,
            system_prompt=self.config.system_prompt,
            briefing=briefing,
            broker=self.broker,
            now=self.run.started_at,
            meter=self.meter,
        )
        stats = self._stats(bars, agent)
        buys = _proposed_buys(agent)
        names = self.broker.get_asset_names(buys) if buys else {}
        verdicts = evaluate(
            [parsed.proposal for parsed in agent.submission.proposals],
            account,
            stats,
            ctx,
            self.policy,
            names,
        )
        proposal_ids = self._record_decisions(agent, verdicts)
        orders = self._send_orders(verdicts, proposal_ids)
        usage = self.meter.usage
        with self.engine.begin() as conn:
            repo.finish_run(
                conn,
                self.run_id,
                finished_at=self.clock(),
                briefing=briefing,
                market_view=agent.submission.market_view,
                agent_submitted=agent.submitted,
                agent_turns=agent.turns,
                usage=usage,
            )
        summary = format_summary(
            run=self.run,
            status=RunStatus.COMPLETED,
            account=account,
            usage=usage,
            turns=agent.turns,
            submitted=agent.submitted,
            market_view=agent.submission.market_view,
            verdicts=verdicts,
            malformed=agent.submission.malformed,
            orders=orders,
            unprotected=self.unprotected,
        )
        log.info("run completed", extra={"run_id": self.run_id, "summary": summary})
        return RunResult(run_id=self.run_id, status=RunStatus.COMPLETED, summary=summary, briefing=briefing)

    # ---- Steps ---------------------------------------------------------------------------------------------

    def _cancel_stale_entries(self) -> None:
        """HANDOFF §3 step 1: earlier runs' unfilled entries, whose legs go with them."""
        if not self.writes_to_broker:
            return
        cancelled = self.broker.cancel_open_buy_orders()
        with self.engine.begin() as conn:
            for order in cancelled:
                repo.record_cancelled_order(
                    conn,
                    self.run_id,
                    broker_order_id=order.broker_order_id,
                    symbol=order.symbol,
                    reason=CancelReason.STALE_ENTRY,
                )
        # A partially filled entry's legs aren't active yet, and cancelling it cancels them: the shares it
        # bought are left with no stop (HANDOFF §8 and §20).
        self.unprotected = [order for order in cancelled if order.filled_qty > 0]
        for order in self.unprotected:
            log.warning(
                "cancelled a partially filled entry, leaving its shares with no stop",
                extra={
                    "run_id": self.run_id,
                    "symbol": order.symbol,
                    "filled_qty": order.filled_qty,
                    "broker_order_id": order.broker_order_id,
                },
            )

    def _read_account(self) -> AccountState:
        account = self.broker.get_account()
        with self.engine.begin() as conn:
            repo.record_account_snapshot(conn, self.run_id, account, taken_at=self.clock())
        return account

    def _risk_context(self) -> RiskContext:
        if self.run.mode is RunMode.OFFLINE:
            return RiskContext()  # the fake account has no history, so repeated offline runs agree
        with self.engine.connect() as conn:
            return repo.risk_context(
                conn,
                run_date=self.run.run_date,
                paper=self.run.paper,
                peak_since=self.policy.drawdown_peak_since,
            )

    def _briefing(self, account: AccountState, ctx: RiskContext) -> tuple[str, dict[str, list[Bar]]]:
        strategy = self.strategy
        bars = self.broker.get_daily_bars(self.universe, BRIEFING_SESSIONS)
        since = self.run.started_at - timedelta(hours=strategy.news_lookback_hours)
        market_news = self.broker.get_news(None, since, MARKET_NEWS_LIMIT)
        held = [position.symbol for position in account.positions]
        position_news = self.broker.get_news(held, since, POSITION_NEWS_LIMIT) if held else []
        briefing = build_briefing(
            run_date=self.run.run_date,
            account=account,
            ctx=ctx,
            policy=self.policy,
            strategy=strategy,
            bars=bars,
            position_news=position_news,
            market_news=market_news,
        )
        return briefing, bars

    def _stats(self, bars: Mapping[str, Sequence[Bar]], agent: AgentResult) -> dict[str, SymbolStats]:
        """The risk engine's market data: the briefing's ETFs, the model's research, then any other buys.

        A symbol whose last bar is older than the latest session in the briefing's bars, such as a halted
        or delisted stock, gets no stats, so the engine rejects buying it instead of sizing it on an old close
        (HANDOFF §7 step 3). Every ETF in the briefing trades every session, so that's the last completed one.
        """
        universe = symbol_stats(bars)
        stats = universe | agent.stats
        seen = {*self.universe, *agent.researched}
        unseen = [symbol for symbol in _proposed_buys(agent) if symbol not in seen]
        if unseen:
            stats |= symbol_stats(self.broker.get_daily_bars(unseen, BUY_SESSIONS))
        if not universe:
            return stats
        last_session = max(stat.as_of for stat in universe.values())
        for stat in stats.values():
            if stat.as_of < last_session:
                log.warning(
                    "dropped stale price history",
                    extra={"symbol": stat.symbol, "as_of": stat.as_of, "last_session": last_session},
                )
        return {symbol: stat for symbol, stat in stats.items() if stat.as_of >= last_session}

    def _record_decisions(self, agent: AgentResult, verdicts: Sequence[Verdict]) -> list[int]:
        with self.engine.begin() as conn:
            for call in agent.tool_calls:
                repo.record_tool_call(
                    conn, self.run_id, seq=call.seq, name=call.name, tool_input=call.input, result=call.result
                )
            proposal_ids = [
                repo.record_proposal(
                    conn,
                    self.run_id,
                    seq=parsed.seq,
                    proposal=parsed.proposal,
                    raw=parsed.raw,
                    verdict=verdict,
                )
                for parsed, verdict in zip(agent.submission.proposals, verdicts, strict=True)
            ]
            for malformed in agent.submission.malformed:
                repo.record_malformed_proposal(conn, self.run_id, raw=malformed.raw, error=malformed.error)
        return proposal_ids

    def _send_orders(self, verdicts: Sequence[Verdict], proposal_ids: Sequence[int]) -> list[OrderLine]:
        """Exits first, then entries (HANDOFF §3 step 9), each side in proposal order."""
        lines: list[OrderLine] = []
        for side in (Side.SELL, Side.BUY):
            for verdict, proposal_id in zip(verdicts, proposal_ids, strict=True):
                if verdict.order is not None and verdict.order.side is side:
                    lines.append(self._send(verdict.order, verdict.opens_new_position, proposal_id))
        return lines

    def _send(self, order: Order, opens_new_position: bool, proposal_id: int) -> OrderLine:
        client_order_id = f"llmt-{self.run.run_date.isoformat()}-{order.symbol}-{order.side.value}"

        def record(status: OrderStatus, **outcome: str) -> OrderLine:
            with self.engine.begin() as conn:
                repo.record_order(
                    conn,
                    self.run_id,
                    proposal_id=proposal_id,
                    client_order_id=client_order_id,
                    order=order,
                    opens_new_position=opens_new_position,
                    status=status,
                    broker_order_id=outcome.get("broker_order_id"),
                    broker_status=outcome.get("broker_status"),
                    not_submitted_reason=outcome.get("not_submitted_reason"),
                    error=outcome.get("error"),
                )
            detail = ", ".join(outcome.values())
            return OrderLine(
                side=order.side, symbol=order.symbol, qty=order.qty, status=status, detail=detail
            )

        if not self.writes_to_broker:
            reason = KILL_SWITCH if not self.policy.trading_enabled else DRY_RUN
            return record(OrderStatus.NOT_SUBMITTED, not_submitted_reason=reason)
        try:
            if order.side is Side.SELL:
                self._cancel_exit_legs(order.symbol)
            receipt = self.broker.submit(order, client_order_id)
        except BrokerError as exc:  # the broker failed or refused this order; the others still go
            error = f"{type(exc).__name__}: {exc}"
            log.error("order failed", extra={"client_order_id": client_order_id, "error": error})
            return record(OrderStatus.ERROR, error=error)
        return record(
            OrderStatus.SUBMITTED, broker_order_id=receipt.broker_order_id, broker_status=receipt.status
        )

    def _cancel_exit_legs(self, symbol: str) -> None:
        """An exit first cancels the position's stop and take-profit, which hold its shares (HANDOFF §8)."""
        legs = self.broker.cancel_open_orders(symbol)
        with self.engine.begin() as conn:
            for broker_order_id in legs:
                repo.record_cancelled_order(
                    conn,
                    self.run_id,
                    broker_order_id=broker_order_id,
                    symbol=symbol,
                    reason=CancelReason.EXIT_LEGS,
                )


def _proposed_buys(agent: AgentResult) -> list[str]:
    """The valid symbols the model proposed buying, each once, sorted."""
    return sorted(
        {
            symbol
            for parsed in agent.submission.proposals
            if parsed.proposal.action is Side.BUY
            and (symbol := normalize_symbol(parsed.proposal.symbol)) is not None
        }
    )


def _abandon_stale_run(conn: Connection, run: NewRun) -> None:
    """--force: mark the day's running submit run abandoned, if it's old enough to be dead (HANDOFF §9)."""
    running = repo.running_submit_run(conn, run_date=run.run_date, paper=run.paper)
    if running is None:
        return
    age = run.started_at - running.started_at
    if age < FORCE_MIN_AGE:
        minutes, needed = age.total_seconds() / 60, FORCE_MIN_AGE.total_seconds() / 60
        raise ForceRefusedError(
            f"submit run {running.run_id} started {minutes:.0f} minutes ago and may still be running; "
            f"--force only abandons one that started at least {needed:.0f} minutes ago"
        )
    repo.abandon_stale_submit_run(conn, run_date=run.run_date, paper=run.paper, at=run.started_at)
    log.warning("abandoned a stale submit run", extra={"run_id": running.run_id})


def _skipped(run_id: UUID, run: NewRun, reason: str) -> RunResult:
    log.info("run skipped", extra={"run_id": run_id, "reason": reason})
    return RunResult(
        run_id=run_id,
        status=RunStatus.SKIPPED,
        summary=f"{run.run_date.isoformat()} · {_mode(run)} · skipped: {reason}",
    )


def _record_failure(
    engine: Engine, run_id: UUID, clock: Callable[[], datetime], exc: BaseException, usage: Usage
) -> None:
    error = f"{type(exc).__name__}: {exc}"
    log.error("run failed", extra={"run_id": run_id, "error": error})
    try:
        with engine.begin() as conn:
            repo.fail_run(conn, run_id, finished_at=clock(), error=error, usage=usage)
    except Exception:  # never hide the error being recorded behind a new one
        log.exception("could not mark run %s failed", run_id)


# ---- The summary -------------------------------------------------------------------------------------------


def format_summary(
    *,
    run: NewRun,
    status: RunStatus,
    account: AccountState,
    usage: Usage,
    turns: int,
    submitted: bool,
    market_view: str | None,
    verdicts: Sequence[Verdict],
    malformed: Sequence[MalformedProposal],
    orders: Sequence[OrderLine],
    unprotected: Sequence[CancelledOrder] = (),
) -> str:
    """The one-screen summary a run ends with (HANDOFF §9): date, mode, equity, cost, prompt version,
    market view, then one line per verdict and per order, and shares a cancelled entry left with no stop."""
    lines = [
        f"{run.run_date.isoformat()} · {_mode(run)} · {status.value}",
        f"Equity {_usd(account.equity)} · cash {_usd(account.cash)} · prompt {run.prompt_version}",
        f"Cost ${usage.cost_usd:.4f} over {turns} model turns: {usage.input_tokens:,} input, "
        f"{usage.output_tokens:,} output, {usage.cache_write_tokens:,} cache write and "
        f"{usage.cache_read_tokens:,} cache read tokens",
        f"Market view: {market_view or 'none given'}"
        if submitted
        else "The model never called submit_proposals, so there are no trades.",
        "Verdicts:" if verdicts else "Verdicts: none",
        *(f"  {_verdict_line(verdict)}" for verdict in verdicts),
    ]
    if malformed:
        lines += [f"Malformed proposals: {len(malformed)}", *(f"  {item.error}" for item in malformed)]
    lines += [
        "Orders:" if orders else "Orders: none",
        *(
            f"  {line.side.value} {line.qty} {line.symbol}: {line.status.value} ({line.detail})"
            for line in orders
        ),
    ]
    if unprotected:
        lines += [
            "Shares with no stop, from partially filled entries cancelled with their stops:",
            *(
                f"  {order.symbol}: {order.filled_qty:g} shares (entry {order.broker_order_id})"
                for order in unprotected
            ),
        ]
    return "\n".join(lines)


def _verdict_line(verdict: Verdict) -> str:
    parts = [f"{verdict.action} {verdict.symbol}: {verdict.status.value}"]
    order = verdict.order
    if order is not None and order.side is Side.BUY:
        prices = f"{order.qty} at limit {_usd(order.limit_price)}, stop {_usd(order.stop_price)}"
        if order.take_profit_price is not None:
            prices += f", take-profit {_usd(order.take_profit_price)}"
        parts.append(prices)
    elif order is not None:
        parts.append(f"{order.qty} at market")
    return " · ".join([*parts, *verdict.reasons])


def _mode(run: NewRun) -> str:
    return f"{run.mode.value} ({'paper' if run.paper else 'LIVE'})"


def _usd(amount: float | None) -> str:
    number = finite_float(amount)
    return "n/a" if number is None else f"${number:,.2f}"
