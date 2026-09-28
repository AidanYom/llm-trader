"""Every query the app runs (HANDOFF §9 and §10), as explicit functions over SQLAlchemy Core.

Each function takes a Connection and never commits: the caller owns the transaction, so run.py can commit
in the order HANDOFF §9 sets out. Floats become Decimal here and nowhere else, and values Postgres refuses
are made storable here (HANDOFF §10):

- A NaN or infinity bound for a NUMERIC column is written as NULL, meaning unknown. Postgres sorts NaN
  above every number, so a stored NaN equity would become the peak and block every buy.
- NUL characters in text are replaced with U+FFFD. Postgres refuses them, and news text is untrusted.
- NaN and infinities inside JSONB become the strings "NaN", "Infinity" and "-Infinity".
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from trader.db.tables import (
    ONE_SUBMIT_PER_DAY,
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
from trader.models import (
    AccountState,
    CancelReason,
    Order,
    OrderStatus,
    Position,
    Proposal,
    RiskContext,
    RunMode,
    RunStatus,
    Side,
    Usage,
    Verdict,
    VerdictStatus,
    finite_float,
)

RESULT_EXCERPT_CHARS = 1500  # how much of each tool result tool_calls keeps (HANDOFF §10)


class SchemaVersionError(RuntimeError):
    """The database isn't at the migration revision this code was written for."""


class RunStateError(RuntimeError):
    """A run's row isn't in the state an update needs, such as completing a run that was abandoned."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NewRun:
    """The fields a run's row starts with, whether the run goes ahead or is skipped."""

    run_date: date  # the America/New_York date
    started_at: datetime  # timezone-aware
    mode: RunMode
    paper: bool
    model: str
    prompt_version: str | None


def check_schema(conn: Connection) -> None:
    """HANDOFF §9's schema guard: raise SchemaVersionError unless the database is at SCHEMA_HEAD."""
    if conn.execute(text("SELECT to_regclass('alembic_version') IS NULL")).scalar_one():
        found = "never been migrated"
    else:
        revisions = sorted(map(str, conn.execute(text("SELECT version_num FROM alembic_version")).scalars()))
        if revisions == [SCHEMA_HEAD]:
            return
        found = f"revision {', '.join(revisions)}" if revisions else "no revision"
    raise SchemaVersionError(
        f"the database has {found}, but this code needs revision {SCHEMA_HEAD}: migrate the database "
        "(make migrate, or make migrate-prod for production) or run code that matches it"
    )


# ---- Runs -------------------------------------------------------------------------------------------------


def save_prompt_version(conn: Connection, *, version: str, system_prompt: str) -> None:
    """Store a prompt's full text under its version, unless that version is already stored (HANDOFF §4)."""
    conn.execute(
        insert(prompt_versions)
        .values(version=version, system_prompt=system_prompt)
        .on_conflict_do_nothing(index_elements=[prompt_versions.c.version])
    )


def start_run(conn: Connection, run: NewRun) -> UUID | None:
    """Insert the run's row as `running` and return its ID.

    Returns None, inserting nothing, when uq_runs_one_submit_per_day refuses it: a submit run for the same
    (run_date, paper) is already running or completed (HANDOFF §9). The caller then records a skipped run.
    """
    run_id = conn.execute(
        insert(runs)
        .values({**_run_row(run), "status": RunStatus.RUNNING.value})
        .on_conflict_do_nothing(
            index_elements=[runs.c.run_date, runs.c.paper], index_where=ONE_SUBMIT_PER_DAY
        )
        .returning(runs.c.id)
    ).scalar()
    return None if run_id is None else _uuid(run_id)


def record_skipped_run(conn: Connection, run: NewRun, *, reason: str) -> UUID:
    """Insert a run that stopped before doing anything, such as on a market holiday (HANDOFF §9)."""
    row = {**_run_row(run), "status": RunStatus.SKIPPED.value, "skip_reason": reason}
    row["finished_at"] = row["started_at"]
    return _uuid(conn.execute(insert(runs).values(row).returning(runs.c.id)).scalar_one())


def abandon_stale_submit_run(conn: Connection, *, run_date: date, paper: bool, at: datetime) -> UUID | None:
    """`--force`: mark the day's `running` submit run `abandoned`, for example after a Lambda timeout.

    Returns its ID, or None when there's none. A completed run is never touched, so it still blocks a second
    one (HANDOFF §9).
    """
    run_id = conn.execute(
        update(runs)
        .where(
            runs.c.run_date == _day(run_date),
            runs.c.paper == paper,
            runs.c.mode == RunMode.SUBMIT.value,
            runs.c.status == RunStatus.RUNNING.value,
        )
        .values(status=RunStatus.ABANDONED.value, finished_at=_aware(at))
        .returning(runs.c.id)
    ).scalar()  # at most one row: uq_runs_one_submit_per_day allows one running submit run a day
    return None if run_id is None else _uuid(run_id)


def finish_run(
    conn: Connection,
    run_id: UUID,
    *,
    finished_at: datetime,
    briefing: str,
    market_view: str | None,
    agent_submitted: bool,
    agent_turns: int,
    usage: Usage,
) -> None:
    """Write the run's summary and mark it completed: the last step of HANDOFF §9's persistence order.

    Raises RunStateError unless the run is still `running`, so a run abandoned by `--force` can't complete
    behind the run that replaced it.
    """
    result = conn.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.status == RunStatus.RUNNING.value)
        .values(
            status=RunStatus.COMPLETED.value,
            finished_at=_aware(finished_at),
            briefing=_text(briefing),
            market_view=_optional_text(market_view),
            agent_submitted=agent_submitted,
            agent_turns=agent_turns,
            **_usage_values(usage),
        )
    )
    if result.rowcount != 1:
        raise RunStateError(f"run {run_id} is not running, so it can't be completed")


def fail_run(
    conn: Connection, run_id: UUID, *, finished_at: datetime, error: str, usage: Usage | None = None
) -> None:
    """Mark a running run `failed`, with the error's text and the API usage so far (HANDOFF §9).

    A run in any other state is left alone, without raising: this runs in run_daily's exception handler,
    where a new error would hide the one being recorded.
    """
    values: dict[str, object] = {
        "status": RunStatus.FAILED.value,
        "finished_at": _aware(finished_at),
        "error": _text(error),
    }
    if usage is not None:
        values |= _usage_values(usage)
    conn.execute(
        update(runs).where(runs.c.id == run_id, runs.c.status == RunStatus.RUNNING.value).values(values)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunningRun:
    run_id: UUID
    started_at: datetime


def running_submit_run(conn: Connection, *, run_date: date, paper: bool) -> RunningRun | None:
    """The day's `running` submit run, if there is one: `--force` checks its age before abandoning it."""
    row = conn.execute(
        select(runs.c.id, runs.c.started_at).where(
            runs.c.run_date == _day(run_date),
            runs.c.paper == paper,
            runs.c.mode == RunMode.SUBMIT.value,
            runs.c.status == RunStatus.RUNNING.value,
        )
    ).first()  # at most one row: uq_runs_one_submit_per_day allows one running submit run a day
    if row is None:
        return None
    started_at: object = row.started_at
    if not isinstance(started_at, datetime):
        raise TypeError(f"expected a timestamp from the database, got {started_at!r}")
    return RunningRun(run_id=_uuid(row.id), started_at=started_at)


# ---- What a run saw and decided -----------------------------------------------------------------------


def record_account_snapshot(
    conn: Connection, run_id: UUID, account: AccountState, *, taken_at: datetime
) -> None:
    """Store the account and each position as the broker reported them."""
    conn.execute(
        insert(account_snapshots).values(
            run_id=run_id,
            equity=_decimal(account.equity),
            cash=_decimal(account.cash),
            taken_at=_aware(taken_at),
        )
    )
    if account.positions:
        conn.execute(
            insert(position_snapshots),
            [
                {
                    "run_id": run_id,
                    "symbol": position.symbol,
                    "qty": _decimal(position.qty),
                    "avg_entry_price": _decimal(position.avg_entry_price),
                    "current_price": _decimal(position.current_price),
                    "market_value": _decimal(position.market_value),
                    "unrealized_plpc": _decimal(position.unrealized_plpc),
                }
                for position in account.positions
            ],
        )


def record_tool_call(
    conn: Connection, run_id: UUID, *, seq: int, name: str, tool_input: object, result: str | None
) -> None:
    """Store one of the model's tool calls, with the start of its result.

    `result` is None for a call that got no result: `submit_proposals`, and any call ignored alongside it.
    """
    conn.execute(
        insert(tool_calls).values(
            run_id=run_id,
            seq=seq,
            name=_text(name),
            input=_json(tool_input),
            result_excerpt=None if result is None else _text(result[:RESULT_EXCERPT_CHARS]),
        )
    )


def record_proposal(
    conn: Connection,
    run_id: UUID,
    *,
    seq: int,
    proposal: Proposal,
    raw: Mapping[str, object],
    verdict: Verdict,
) -> int:
    """Store a proposal, as parsed and as the model sent it, and the risk engine's verdict on it.

    Returns the proposal's ID, which its order refers to.
    """
    proposal_id = _int(
        conn.execute(
            insert(proposals)
            .values(
                run_id=run_id,
                seq=seq,
                symbol=_text(proposal.symbol),
                action=Side(proposal.action).value,
                target_pct=_decimal(proposal.target_pct),
                stop_pct=_decimal(proposal.stop_pct),
                take_profit_pct=_decimal(proposal.take_profit_pct),
                thesis=_text(proposal.thesis),
                invalidation=_text(proposal.invalidation),
                confidence=_decimal(proposal.confidence),
                raw=_json(raw),
            )
            .returning(proposals.c.id)
        ).scalar_one()
    )
    order = verdict.order  # None for a rejection
    conn.execute(
        insert(verdicts).values(
            proposal_id=proposal_id,
            status=verdict.status.value,
            reasons=_json(list(verdict.reasons)),
            opens_new_position=verdict.opens_new_position,
            qty=None if order is None else order.qty,
            limit_price=None if order is None else _decimal(order.limit_price),
            stop_price=None if order is None else _decimal(order.stop_price),
            take_profit_price=None if order is None else _decimal(order.take_profit_price),
        )
    )
    return proposal_id


def record_malformed_proposal(conn: Connection, run_id: UUID, *, raw: object, error: str) -> None:
    """Store a proposal the parser couldn't use, as the model sent it (HANDOFF §5)."""
    conn.execute(insert(malformed_proposals).values(run_id=run_id, raw=_json(raw), error=_text(error)))


def record_order(
    conn: Connection,
    run_id: UUID,
    *,
    proposal_id: int,
    client_order_id: str,
    order: Order,
    opens_new_position: bool,
    status: OrderStatus,
    broker_order_id: str | None = None,
    broker_status: str | None = None,
    not_submitted_reason: str | None = None,
    error: str | None = None,
) -> int:
    """Store an order and what became of it, and return its ID.

    run.py commits each order right after its broker call, so a crash can't lose the record of a submitted
    order (HANDOFF §9).
    """
    order_id: object = conn.execute(
        insert(orders)
        .values(
            run_id=run_id,
            proposal_id=proposal_id,
            client_order_id=client_order_id,
            symbol=order.symbol,
            side=Side(order.side).value,
            qty=order.qty,
            limit_price=_decimal(order.limit_price),
            stop_price=_decimal(order.stop_price),
            take_profit_price=_decimal(order.take_profit_price),
            opens_new_position=opens_new_position,
            status=status.value,
            not_submitted_reason=not_submitted_reason,
            broker_order_id=broker_order_id,
            broker_status=broker_status,
            error=_optional_text(error),
        )
        .returning(orders.c.id)
    ).scalar_one()
    return _int(order_id)


def record_cancelled_order(
    conn: Connection, run_id: UUID, *, broker_order_id: str, symbol: str | None, reason: CancelReason
) -> None:
    """Store an order the run cancelled: an earlier run's unfilled entry, or an exit's stop and take-profit.

    `symbol` is None when the broker call returned only the order's ID.
    """
    conn.execute(
        insert(cancelled_orders).values(
            run_id=run_id, broker_order_id=broker_order_id, symbol=symbol, reason=reason.value
        )
    )


# ---- Risk context ----------------------------------------------------------------------------------------


def risk_context(conn: Connection, *, run_date: date, paper: bool, peak_since: date | None) -> RiskContext:
    """What the risk engine needs from earlier runs, as of `run_date` (HANDOFF §9).

    Reads every dry_run and submit run with this `paper` flag, whatever its status: a run that failed or was
    abandoned can still have sent real orders, and its snapshot is a real account read. Offline runs are
    left out, and so are runs dated after `run_date`.

    - equity_peak: the highest snapshot equity from runs dated on or after `peak_since` (all history when
      it's None), or None when there's none. Unknown (NULL) equities are skipped. The engine compares the
      peak with current equity itself.
    - new_positions_this_week: submitted orders that open a position, from runs dated from this week's
      Monday through `run_date`. Entries that never filled still count.
    """
    day = _day(run_date)
    counted = (
        runs.c.mode.in_([RunMode.DRY_RUN.value, RunMode.SUBMIT.value]),
        runs.c.paper == paper,
        runs.c.run_date <= day,
    )
    peak = (
        select(func.max(account_snapshots.c.equity)).select_from(account_snapshots.join(runs)).where(*counted)
    )
    if peak_since is not None:
        peak = peak.where(runs.c.run_date >= _day(peak_since))
    monday = day - timedelta(days=day.weekday())
    new_positions = (
        select(func.count())
        .select_from(orders.join(runs))
        .where(
            *counted,
            runs.c.run_date >= monday,
            orders.c.status == OrderStatus.SUBMITTED.value,
            orders.c.opens_new_position.is_(True),
        )
    )
    return RiskContext(
        new_positions_this_week=_int(conn.execute(new_positions).scalar_one()),
        equity_peak=_optional_float(conn.execute(peak).scalar_one()),
    )


# ---- The weekly report (HANDOFF §11) -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportRun:
    run_id: UUID
    run_date: date
    started_at: datetime
    mode: RunMode
    status: RunStatus
    skip_reason: str | None
    error: str | None
    model: str
    prompt_version: str | None
    market_view: str | None
    agent_submitted: bool | None
    cost_usd: float
    equity: float | None  # from the run's account snapshot; None without one, or when it was unknown


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportProposal:
    run_id: UUID
    seq: int
    symbol: str
    action: Side
    target_pct: float | None
    stop_pct: float | None
    thesis: str
    invalidation: str
    status: VerdictStatus
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportMalformed:
    run_id: UUID
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportOrder:
    run_id: UUID
    symbol: str
    side: Side
    qty: int
    status: OrderStatus


def report_runs(
    conn: Connection, *, first_day: date, last_day: date, paper: bool, modes: Collection[RunMode]
) -> list[ReportRun]:
    """Every run dated in the window for this account type and these modes, whatever its status.

    They come in start order, each with the equity of its account snapshot.
    """
    query = (
        select(runs, account_snapshots.c.equity)
        .select_from(runs.outerjoin(account_snapshots))
        .where(
            runs.c.run_date.between(_day(first_day), _day(last_day)),
            runs.c.paper == paper,
            runs.c.mode.in_([RunMode(mode).value for mode in modes]),
        )
        .order_by(runs.c.started_at, runs.c.id)
    )
    return [
        ReportRun(
            run_id=_uuid(row.id),
            run_date=_date(row.run_date),
            started_at=_datetime(row.started_at),
            mode=RunMode(row.mode),
            status=RunStatus(row.status),
            skip_reason=_optional_str(row.skip_reason),
            error=_optional_str(row.error),
            model=_str(row.model),
            prompt_version=_optional_str(row.prompt_version),
            market_view=_optional_str(row.market_view),
            agent_submitted=_optional_bool(row.agent_submitted),
            cost_usd=_float(row.cost_usd),
            equity=_optional_float(row.equity),
        )
        for row in conn.execute(query)
    ]


def report_proposals(conn: Connection, run_ids: Sequence[UUID]) -> list[ReportProposal]:
    """The runs' proposals with their verdicts, in each run's seq order."""
    query = (
        select(proposals, verdicts.c.status.label("verdict_status"), verdicts.c.reasons)
        .join(verdicts)
        .where(proposals.c.run_id.in_(run_ids))
        .order_by(proposals.c.run_id, proposals.c.seq)
    )
    return [
        ReportProposal(
            run_id=_uuid(row.run_id),
            seq=_int(row.seq),
            symbol=_str(row.symbol),
            action=Side(row.action),
            target_pct=_optional_float(row.target_pct),
            stop_pct=_optional_float(row.stop_pct),
            thesis=_str(row.thesis),
            invalidation=_str(row.invalidation),
            status=VerdictStatus(row.verdict_status),
            reasons=_strings(row.reasons),
        )
        for row in conn.execute(query)
    ]


def report_malformed(conn: Connection, run_ids: Sequence[UUID]) -> list[ReportMalformed]:
    query = (
        select(malformed_proposals.c.run_id, malformed_proposals.c.error)
        .where(malformed_proposals.c.run_id.in_(run_ids))
        .order_by(malformed_proposals.c.id)
    )
    return [ReportMalformed(run_id=_uuid(row.run_id), error=_str(row.error)) for row in conn.execute(query)]


def report_orders(conn: Connection, run_ids: Sequence[UUID]) -> list[ReportOrder]:
    query = (
        select(orders.c.run_id, orders.c.symbol, orders.c.side, orders.c.qty, orders.c.status)
        .where(orders.c.run_id.in_(run_ids))
        .order_by(orders.c.id)
    )
    return [
        ReportOrder(
            run_id=_uuid(row.run_id),
            symbol=_str(row.symbol),
            side=Side(row.side),
            qty=_int(row.qty),
            status=OrderStatus(row.status),
        )
        for row in conn.execute(query)
    ]


def report_research_calls(conn: Connection, run_ids: Sequence[UUID]) -> dict[UUID, int]:
    """Each run's research tool calls: every tool call but submit_proposals. Runs with none are left out."""
    query = (
        select(tool_calls.c.run_id, func.count())
        .where(tool_calls.c.run_id.in_(run_ids), tool_calls.c.name != "submit_proposals")
        .group_by(tool_calls.c.run_id)
    )
    return {_uuid(run_id): _int(calls) for run_id, calls in conn.execute(query)}


def report_positions(conn: Connection, run_id: UUID) -> list[Position]:
    """The positions a run's snapshot recorded, largest market value first. Unknown numbers are NaN."""
    query = (
        select(position_snapshots)
        .where(position_snapshots.c.run_id == run_id)
        .order_by(position_snapshots.c.market_value.desc().nulls_last(), position_snapshots.c.symbol)
    )
    return [
        Position(
            symbol=_str(row.symbol),
            qty=_float_or_nan(row.qty),
            avg_entry_price=_float_or_nan(row.avg_entry_price),
            current_price=_float_or_nan(row.current_price),
            market_value=_float_or_nan(row.market_value),
            unrealized_plpc=_float_or_nan(row.unrealized_plpc),
        )
        for row in conn.execute(query)
    ]


# ---- Conversions at the database boundary ----------------------------------------------------------------


def _usage_values(usage: Usage) -> dict[str, object]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cost_usd": _decimal(usage.cost_usd),
    }


def _run_row(run: NewRun) -> dict[str, object]:
    return {
        "run_date": _day(run.run_date),
        "started_at": _aware(run.started_at),
        "mode": RunMode(run.mode).value,
        "paper": run.paper,
        "model": run.model,
        "prompt_version": run.prompt_version,
    }


def _decimal(value: object) -> Decimal | None:
    """The number as a Decimal for a NUMERIC column; None (NULL) when it's missing or not finite."""
    number = finite_float(value)
    return None if number is None else Decimal(str(number))


def _text(value: str) -> str:
    """The text with each NUL character, which Postgres refuses, replaced."""
    return value.replace("\x00", "\N{REPLACEMENT CHARACTER}")


def _optional_text(value: str | None) -> str | None:
    return None if value is None else _text(value)


def _json(value: object) -> object:
    """A JSON value Postgres accepts: non-finite floats become strings, and NULs in text are replaced."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, Mapping):
        return {_text(key) if isinstance(key, str) else key: _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return value


def _aware(moment: datetime) -> datetime:
    """The datetime, which must carry a time zone: Postgres would read a naive one as UTC."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no time zone")
    return moment


def _day(day: date) -> date:
    """The run date, which must be a date: a datetime would compare as a moment, not a day."""
    if isinstance(day, datetime):
        raise TypeError(f"expected an America/New_York date, got the datetime {day!r}")
    return day


def _uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"expected a UUID from the database, got {value!r}")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer from the database, got {value!r}")
    return value


def _optional_float(value: object) -> float | None:
    """A NUMERIC value, read back as the float the domain uses."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"expected a NUMERIC value from the database, got {value!r}")
    return float(value)


def _float(value: object) -> float:
    number = _optional_float(value)
    if number is None:
        raise TypeError("expected a NUMERIC value from the database, got NULL")
    return number


def _float_or_nan(value: object) -> float:
    """A nullable NUMERIC value, with NULL, meaning unknown, read back as NaN."""
    number = _optional_float(value)
    return math.nan if number is None else number


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected text from the database, got {value!r}")
    return value


def _optional_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"expected a boolean from the database, got {value!r}")
    return value


def _strings(value: object) -> tuple[str, ...]:
    """A JSONB array of strings, such as a verdict's reasons."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"expected a JSON array of strings from the database, got {value!r}")
    return tuple(value)


def _date(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"expected a date from the database, got {value!r}")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected a timestamp from the database, got {value!r}")
    return value
