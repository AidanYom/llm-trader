"""The Claude agent (HANDOFF §5): the read-only tools, the tool loop, proposal parsing and cost.

This is the only module that calls the Anthropic API.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from trader.models import Proposal, Side

PERCENT_LIMIT = 1_000.0  # a percentage this large in magnitude is malformed: it wouldn't fit its column


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
