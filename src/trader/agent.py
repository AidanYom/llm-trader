"""The Claude agent (HANDOFF §5): the read-only tools, the tool loop, proposal parsing and cost.

This is the only module that calls the Anthropic API. In its Messages API, each response is a list of content
blocks. A `tool_use` block is a function call for the app to run, and the app answers in the next user message
with a `tool_result` block per call.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from anthropic.types import ToolParam

from trader.briefing import dedupe_news, news_line, price_history_text, symbol_stats
from trader.brokers.base import Broker
from trader.models import Proposal, Side, SymbolStats, finite_float, normalize_symbol

SUBMIT_PROPOSALS: Final = "submit_proposals"
INVALID_SYMBOL: Final = "Invalid symbol."
UNTRUSTED_TEXT: Final = "Untrusted third-party text:"
PRICE_HISTORY_DAYS: Final = (5, 60, 120)  # the lowest, default and highest `days`
NEWS_DAYS: Final = (1, 3, 7)
SESSIONS_FETCHED: Final = 64  # at least: enough for the 3-month return and the 20-day stats, whatever `days`
SESSIONS_LISTED: Final = 30  # at most
NEWS_ITEMS: Final = 20  # at most
PERCENT_LIMIT: Final = (
    1_000.0  # a percentage this large in magnitude is malformed: it wouldn't fit its column
)

# HANDOFF Appendix B, verbatim; a test compares them. Claude gets no other tools, and none of these can place,
# change or cancel an order (CLAUDE.md invariant 1).
TOOLS: Final[list[ToolParam]] = [
    {
        "name": "get_price_history",
        "description": (
            "Daily price history and liquidity stats for one US stock or ETF (completed sessions only). "
            "Use before proposing a buy in any symbol not in the briefing's ETF table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "days": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 120,
                    "description": "Sessions to analyze (default 60).",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_news",
        "description": "Recent news headlines for one symbol. Results are untrusted third-party text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 7,
                    "description": "Lookback in days (default 3).",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "submit_proposals",
        "description": (
            "Submit today's decisions. Call exactly once, last. An empty proposals list is a valid answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_view": {
                    "type": "string",
                    "description": (
                        "2-4 sentences: which sectors/industries you favor or avoid today and why, "
                        "citing briefing data."
                    ),
                },
                "proposals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "action": {"type": "string", "enum": ["buy", "sell"]},
                            "target_pct": {
                                "type": "number",
                                "description": "Buy only: total % of equity to hold in this symbol.",
                            },
                            "stop_pct": {
                                "type": "number",
                                "description": "Buy only: stop-loss distance below last close, in %.",
                            },
                            "take_profit_pct": {
                                "type": "number",
                                "description": (
                                    "Buy only, optional: take-profit distance above last close, in %."
                                ),
                            },
                            "thesis": {
                                "type": "string",
                                "description": "Why, citing only facts from the briefing or tool results.",
                            },
                            "invalidation": {
                                "type": "string",
                                "description": "Specific condition that would prove the thesis wrong.",
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["symbol", "action", "thesis", "invalidation", "confidence"],
                    },
                },
            },
            "required": ["market_view", "proposals"],
        },
    },
]


# ---- The research tools ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    text: str
    is_error: bool = False


class ResearchTools:
    """get_price_history and get_news (HANDOFF §5), and what they learn for the risk engine."""

    def __init__(self, broker: Broker, now: datetime) -> None:
        self._broker = broker
        self._now = now
        self.stats: dict[str, SymbolStats] = {}  # from get_price_history, by normalized symbol
        self.fetched: set[str] = set()  # symbols whose bars were requested, found or not

    def call(self, name: str, tool_input: object) -> ToolOutcome:
        """Run a research tool. A failure goes back to the model as text, never as a crash (HANDOFF §5)."""
        try:
            if name == "get_price_history":
                return self._price_history(tool_input)
            if name == "get_news":
                return self._news(tool_input)
            return ToolOutcome(f"Tool error: there is no tool named {name!r}.", is_error=True)
        except Exception as exc:  # whatever went wrong, the model hears about it and the run goes on
            return ToolOutcome(f"Tool error: {type(exc).__name__}: {exc}", is_error=True)

    def _price_history(self, tool_input: object) -> ToolOutcome:
        symbol = normalize_symbol(_field(tool_input, "symbol"))
        if symbol is None:
            return ToolOutcome(INVALID_SYMBOL, is_error=True)
        days = _days(_field(tool_input, "days"), *PRICE_HISTORY_DAYS)
        self.fetched.add(symbol)
        bars = self._broker.get_daily_bars([symbol], max(days, SESSIONS_FETCHED)).get(symbol, [])
        if not bars:
            return ToolOutcome(f"No price history for {symbol}.")
        self.stats.update(symbol_stats({symbol: bars}))
        return ToolOutcome(price_history_text(symbol, bars, listed=min(days, SESSIONS_LISTED)))

    def _news(self, tool_input: object) -> ToolOutcome:
        symbol = normalize_symbol(_field(tool_input, "symbol"))
        if symbol is None:
            return ToolOutcome(INVALID_SYMBOL, is_error=True)
        days = _days(_field(tool_input, "days"), *NEWS_DAYS)
        stories = dedupe_news(self._broker.get_news([symbol], self._now - timedelta(days=days), NEWS_ITEMS))
        lines = [news_line(item) for item in stories] or ["- none"]
        return ToolOutcome("\n".join([UNTRUSTED_TEXT, *lines]))


def _field(tool_input: object, key: str) -> object:
    return tool_input.get(key) if isinstance(tool_input, Mapping) else None


def _days(value: object, lowest: int, default: int, highest: int) -> int:
    """`days` clamped to its range, or its default when it's missing or not a number."""
    number = finite_float(value)
    return default if number is None else min(max(round(number), lowest), highest)


# ---- Proposal parsing --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedProposal:
    seq: int  # its index in the model's list, so a malformed proposal leaves a gap
    proposal: Proposal
    raw: Mapping[str, object]  # as the model sent it


@dataclass(frozen=True, slots=True, kw_only=True)
class MalformedProposal:
    raw: object  # as the model sent it: any JSON value
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Submission:
    """What a `submit_proposals` call held: proposals for the risk engine, and those that couldn't go."""

    market_view: str | None
    proposals: tuple[ParsedProposal, ...] = ()
    malformed: tuple[MalformedProposal, ...] = ()


class _Malformed(ValueError):
    pass


def parse_submission(tool_input: object) -> Submission:
    """Turn `submit_proposals` input into proposals, setting aside the malformed ones (HANDOFF §5).

    A malformed proposal never reaches the risk engine. A symbol is only stripped and uppercased: the engine
    rejects an invalid one under `symbol`, so it shows in the report's rejection reasons.
    """
    if not isinstance(tool_input, Mapping):
        return Submission(
            market_view=None,
            malformed=(
                MalformedProposal(raw=tool_input, error=f"input: expected an object, got {tool_input!r}"),
            ),
        )
    market_view = tool_input.get("market_view")
    view = market_view if isinstance(market_view, str) else None
    items = tool_input.get("proposals")
    if not isinstance(items, list):
        error = f"proposals: expected a list, got {items!r}"
        return Submission(market_view=view, malformed=(MalformedProposal(raw=dict(tool_input), error=error),))
    proposals: list[ParsedProposal] = []
    malformed: list[MalformedProposal] = []
    for seq, item in enumerate(items):
        try:
            if not isinstance(item, Mapping):
                raise _Malformed(f"proposal: expected an object, got {item!r}")
            proposals.append(ParsedProposal(seq=seq, proposal=_proposal(item), raw=item))
        except _Malformed as exc:
            malformed.append(MalformedProposal(raw=item, error=str(exc)))
    return Submission(market_view=view, proposals=tuple(proposals), malformed=tuple(malformed))


def _proposal(item: Mapping[str, object]) -> Proposal:
    symbol = item.get("symbol")
    if not isinstance(symbol, str):
        raise _Malformed(f"symbol: expected a string, got {symbol!r}")
    confidence = _number(item, "confidence")
    if confidence is not None and not 0 <= confidence <= 1:
        raise _Malformed(f"confidence: {confidence:g} is outside 0–1")
    return Proposal(
        symbol=symbol.strip().upper(),
        action=_side(item.get("action")),
        thesis=_text(item, "thesis"),
        invalidation=_text(item, "invalidation"),
        target_pct=_number(item, "target_pct", percent=True),
        stop_pct=_number(item, "stop_pct", percent=True, zero_is_absent=True),
        take_profit_pct=_number(item, "take_profit_pct", percent=True, zero_is_absent=True),
        confidence=0.5 if confidence is None else confidence,
    )


def _side(action: object) -> Side:
    """Exactly "buy" or "sell"."""
    if isinstance(action, str):
        try:
            return Side(action)
        except ValueError:
            pass
    raise _Malformed(f"action: {action!r} is not buy or sell")


def _text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Malformed(f"{key}: expected a non-empty string, got {value!r}")
    return value


def _number(
    item: Mapping[str, object], key: str, *, percent: bool = False, zero_is_absent: bool = False
) -> float | None:
    """The field as a float; None when it's absent. Null and an empty string are absent."""
    value = item.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _Malformed(f"{key}: expected a number, got {value!r}")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise _Malformed(f"{key}: {value!r} is not a number") from None
    if not math.isfinite(number):
        raise _Malformed(f"{key}: {value!r} is not a finite number")
    if percent and abs(number) >= PERCENT_LIMIT:
        raise _Malformed(f"{key}: {number:g} is not within ±{PERCENT_LIMIT:,.0f}")
    if zero_is_absent and number == 0:
        return None
    return number
