"""Domain types shared by the risk engine, the broker adapters, the agent and the database layer.

Pure data: no I/O and no SDK imports. Every class is an immutable dataclass whose fields are passed by
name. `Order` and `Verdict` check their own invariants when built, so a bug can't produce a buy without a
stop. `Proposal` doesn't: it holds model output, and the risk engine turns a bad field into a rejection
with a reason instead of an exception.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

# Run dates and session dates are New York dates (HANDOFF §10).
NEW_YORK = ZoneInfo("America/New_York")

# HANDOFF §5: letters, digits and dots, at most 10 characters, starting with a letter or digit.
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.]{0,9}")


class Side(StrEnum):
    """A proposal's action and an order's side. Members are plain strings: `Side.BUY == "buy"`."""

    BUY = "buy"
    SELL = "sell"


class VerdictStatus(StrEnum):
    APPROVED = "approved"
    TRIMMED = "trimmed"
    REJECTED = "rejected"


class RunMode(StrEnum):
    """How a run treats orders (HANDOFF §9). The CLI spells DRY_RUN as `dry-run`."""

    OFFLINE = "offline"  # FakeBroker and a scripted model
    DRY_RUN = "dry_run"  # real broker and model; orders recorded, never sent
    SUBMIT = "submit"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"  # stopped before doing anything: market closed, or already ran today
    FAILED = "failed"
    ABANDONED = "abandoned"  # a stale running row, closed by --force


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"  # the broker accepted it
    NOT_SUBMITTED = "not_submitted"  # a dry run or the kill switch: recorded, never sent
    ERROR = "error"  # the broker call failed or the broker refused the order


class CancelReason(StrEnum):
    STALE_ENTRY = "stale_entry"  # an earlier run's unfilled entry, cancelled at the start of a submit run
    EXIT_LEGS = "exit_legs"  # a position's stop and take-profit legs, cancelled before its exit


def new_york_date(moment: datetime) -> date:
    """The America/New_York date of a moment, which must carry a time zone."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no time zone")
    return moment.astimezone(NEW_YORK).date()


def normalize_symbol(raw: object) -> str | None:
    """The ticker stripped and uppercased, or None unless it's 1–10 ASCII letters, digits and dots."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped.isascii():  # "ß".upper() is "SS": only ASCII may be uppercased into a ticker
        return None
    symbol = stripped.upper()
    return symbol if _SYMBOL.fullmatch(symbol) else None


def finite_float(value: object) -> float | None:
    """The value as a finite float. None for None, bools, NaN, infinities and anything that isn't a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        return None
    return number if math.isfinite(number) else None


def to_cents(price: object) -> int | None:
    """The price in whole cents, or None if it isn't a usable number.

    Compare prices this way: as floats, 50.5 + 0.01 and 50.51 aren't guaranteed to be equal.
    """
    number = finite_float(price)
    if number is None or not math.isfinite(number * 100):
        return None
    return round(number * 100)


@dataclass(frozen=True, slots=True, kw_only=True)
class Bar:
    """One completed daily session."""

    day: date  # the session's America/New_York date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True, kw_only=True)
class NewsItem:
    """A news story. Its text is untrusted third-party data, never instructions (CLAUDE.md invariant 9)."""

    created_at: datetime  # timezone-aware, UTC
    headline: str
    summary: str
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Position:
    """An open long position, as the broker reports it."""

    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_plpc: float  # unrealized P&L as a fraction of cost: 0.05 is +5%


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountState:
    equity: float
    cash: float
    positions: tuple[Position, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolStats:
    """What the risk engine needs to know about a symbol, from completed sessions only."""

    symbol: str
    as_of: date  # the last completed session
    last_close: float
    avg_dollar_volume_20d: float  # mean of close × volume over the last 20 sessions


@dataclass(frozen=True, slots=True, kw_only=True)
class Proposal:
    """One decision from `submit_proposals` (HANDOFF §5). The risk engine re-checks every field."""

    symbol: str
    action: Side
    thesis: str
    invalidation: str
    target_pct: float | None = None  # buy: the total % of equity to hold in the symbol
    stop_pct: float | None = None  # buy: stop distance below the last close, in %
    take_profit_pct: float | None = None  # buy, optional: take-profit distance above the last close, in %
    confidence: float = 0.5


@dataclass(frozen=True, slots=True, kw_only=True)
class Order:
    """An order for the broker: a limit buy with a protective stop, or a full exit at market."""

    symbol: str
    side: Side
    qty: int  # whole shares
    limit_price: float | None = None  # None: a market order
    stop_price: float | None = None
    take_profit_price: float | None = None

    def __post_init__(self) -> None:
        # CLAUDE.md invariant 3: long-only, whole shares, and a protective stop on every buy.
        if not self.symbol:
            raise ValueError("an order needs a symbol")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty < 1:
            raise ValueError(f"{self.symbol}: qty must be a whole number of shares >= 1, got {self.qty!r}")
        prices = (self.limit_price, self.stop_price, self.take_profit_price)
        if self.side == Side.SELL:
            if any(price is not None for price in prices):
                raise ValueError(f"{self.symbol}: a sell is a full exit at market and takes no prices")
            return
        if self.side != Side.BUY:
            raise ValueError(f"{self.symbol}: side must be buy or sell, got {self.side!r}")
        limit = to_cents(self.limit_price)
        stop = to_cents(self.stop_price)
        if limit is None or stop is None:
            raise ValueError(f"{self.symbol}: a buy needs a limit price and a protective stop")
        if not 1 <= stop < limit:
            raise ValueError(
                f"{self.symbol}: the stop {self.stop_price!r} must be at least $0.01 and below the limit "
                f"{self.limit_price!r}"
            )
        if self.take_profit_price is not None:
            take_profit = to_cents(self.take_profit_price)
            if take_profit is None or take_profit <= limit:
                raise ValueError(
                    f"{self.symbol}: the take-profit {self.take_profit_price!r} must be above the limit "
                    f"{self.limit_price!r}"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class Verdict:
    """The risk engine's decision on one proposal (HANDOFF §7)."""

    symbol: str  # stripped and uppercased
    action: str  # "buy" or "sell", or the invalid action the model sent
    status: VerdictStatus
    reasons: tuple[str, ...] = ()  # "category: detail"; the weekly report groups on the category
    opens_new_position: bool = False
    order: Order | None = None

    def __post_init__(self) -> None:
        rejected = self.status == VerdictStatus.REJECTED
        if rejected != (self.order is None):
            raise ValueError(f"{self.symbol}: a verdict has an order exactly when it isn't rejected")
        if rejected and self.opens_new_position:
            raise ValueError(f"{self.symbol}: a rejected verdict can't open a position")
        if self.status != VerdictStatus.APPROVED and not self.reasons:
            raise ValueError(f"{self.symbol}: every trim and rejection needs a reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskContext:
    """What the risk engine needs from earlier runs, read from Postgres (HANDOFF §9)."""

    new_positions_this_week: int = 0
    equity_peak: float | None = None  # the highest snapshot equity; None before there is any history


@dataclass(frozen=True, slots=True, kw_only=True)
class Usage:
    """A run's token usage, summed across its model turns, and what it cost (HANDOFF §5)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class StopPolicy:
    required: bool
    min_pct: float
    max_pct: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Policy:
    """Risk limits and switches from `config/policy.yaml`, whose keys match these fields.

    The values are Aidan's: `settings.load_policy()` checks they're usable, not that they're wise.
    """

    trading_enabled: bool
    allow_live_money: bool
    max_position_pct: float
    max_open_positions: int
    max_new_positions_per_week: int
    min_cash_buffer_pct: float
    min_price: float
    min_avg_dollar_volume: float
    max_pct_of_adv: float
    entry_limit_buffer_pct: float
    stop: StopPolicy
    drawdown_freeze_pct: float
    drawdown_peak_since: date | None
    blocked_symbols: frozenset[str]  # uppercased


@dataclass(frozen=True, slots=True, kw_only=True)
class Prices:
    """Claude's prices in USD per million tokens (HANDOFF §5)."""

    input: float
    output: float
    cache_write: float  # 5-minute cache writes
    cache_read: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Strategy:
    """The model, the research budget, the ETF universe and prices, from `config/strategy.yaml`.

    Its keys match these fields. The tickers are uppercased and keep the file's order.
    """

    model: str
    max_tokens: int  # per model call
    max_turns: int  # model calls per run
    max_tool_calls: int  # research tool calls per run
    benchmark: str
    sector_etfs: tuple[str, ...]
    industry_etfs: tuple[str, ...]
    baseline_basket: tuple[str, ...]  # the weekly report's equal-weight baseline
    news_lookback_hours: float
    max_news_items: int  # market headlines in the briefing
    price: Prices
    system_frame: str  # prompt paths, relative to the working directory
    strategy_prompt: str
