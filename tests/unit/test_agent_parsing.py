from __future__ import annotations

import math
from typing import Any

import pytest

from trader.agent import MalformedProposal, parse_submission
from trader.models import Proposal, Side


def buy(**changes: Any) -> dict[str, Any]:
    """A valid buy proposal as the model sends it, with some fields changed or removed (value None)."""
    proposal: dict[str, Any] = {
        "symbol": "XLE",
        "action": "buy",
        "target_pct": 5,
        "stop_pct": 8,
        "take_profit_pct": 15,
        "thesis": "Energy leads on 1m relative strength after OPEC+ cuts.",
        "invalidation": "XLE closes below its 20-day low.",
        "confidence": 0.6,
    }
    proposal.update(changes)
    return {key: value for key, value in proposal.items() if value is not None}


def only_proposal(item: dict[str, Any]) -> Proposal:
    submission = parse_submission({"market_view": "view", "proposals": [item]})
    assert submission.malformed == (), submission.malformed
    (parsed,) = submission.proposals
    return parsed.proposal


def only_error(item: object) -> str:
    submission = parse_submission({"market_view": "view", "proposals": [item]})
    assert submission.proposals == ()
    (malformed,) = submission.malformed
    assert malformed.raw == item  # stored as the model sent it
    return malformed.error


def test_valid_buy_is_parsed() -> None:
    assert only_proposal(buy()) == Proposal(
        symbol="XLE",
        action=Side.BUY,
        thesis="Energy leads on 1m relative strength after OPEC+ cuts.",
        invalidation="XLE closes below its 20-day low.",
        target_pct=5.0,
        stop_pct=8.0,
        take_profit_pct=15.0,
        confidence=0.6,
    )


def test_sell_needs_no_numbers() -> None:
    proposal = only_proposal(
        {"symbol": "SMH", "action": "sell", "thesis": "Played out.", "invalidation": "n/a"}
    )

    assert (proposal.action, proposal.target_pct, proposal.stop_pct, proposal.confidence) == (
        Side.SELL,
        None,
        None,
        0.5,
    )


def test_symbol_is_stripped_and_uppercased_but_left_to_the_engine() -> None:
    assert only_proposal(buy(symbol="  xle ")).symbol == "XLE"
    assert only_proposal(buy(symbol="BRK B")).symbol == "BRK B"  # invalid: the risk engine rejects it


@pytest.mark.parametrize(
    ("changes", "field", "expected"),
    [
        ({"target_pct": "5"}, "target_pct", 5.0),
        ({"target_pct": " 2.5 "}, "target_pct", 2.5),
        ({"target_pct": ""}, "target_pct", None),
        ({"target_pct": 0}, "target_pct", 0.0),  # not absent: the engine rejects a target of 0
        ({"stop_pct": 0}, "stop_pct", None),
        ({"stop_pct": ""}, "stop_pct", None),
        ({"take_profit_pct": 0.0}, "take_profit_pct", None),
        ({"take_profit_pct": None}, "take_profit_pct", None),
        ({"confidence": None}, "confidence", 0.5),
        ({"confidence": "1"}, "confidence", 1.0),
        ({"confidence": 0}, "confidence", 0.0),
        ({"target_pct": 999.99}, "target_pct", 999.99),
        ({"stop_pct": -999.99}, "stop_pct", -999.99),  # the engine rejects it; it fits the column
    ],
)
def test_numbers_are_coerced(changes: dict[str, Any], field: str, expected: float | None) -> None:
    assert getattr(only_proposal(buy(**changes)), field) == expected


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ("buy XLE", "proposal: expected an object, got 'buy XLE'"),
        (buy(symbol=None), "symbol: expected a string, got None"),
        (buy(symbol=5), "symbol: expected a string, got 5"),
        (buy(action="BUY"), "action: 'BUY' is not buy or sell"),
        (buy(action="hold"), "action: 'hold' is not buy or sell"),
        (buy(action=None), "action: None is not buy or sell"),
        (buy(thesis=""), "thesis: expected a non-empty string, got ''"),
        (buy(invalidation="   "), "invalidation: expected a non-empty string, got '   '"),
        (buy(thesis=7), "thesis: expected a non-empty string, got 7"),
        (buy(target_pct="5%"), "target_pct: '5%' is not a number"),
        (buy(target_pct=True), "target_pct: expected a number, got True"),
        (buy(target_pct=[5]), "target_pct: expected a number, got [5]"),
        (buy(stop_pct=math.nan), "stop_pct: nan is not a finite number"),
        (buy(stop_pct="Infinity"), "stop_pct: 'Infinity' is not a finite number"),
        (buy(target_pct=10**400), "target_pct: 1000…"),
        (buy(target_pct=1000), "target_pct: 1000 is not within ±1,000"),
        (buy(take_profit_pct=-1000), "take_profit_pct: -1000 is not within ±1,000"),
        (buy(confidence=1.5), "confidence: 1.5 is outside 0–1"),
        (buy(confidence=-0.1), "confidence: -0.1 is outside 0–1"),
    ],
)
def test_malformed_proposals_are_set_aside_with_the_reason(item: object, error: str) -> None:
    if error.endswith("…"):
        assert only_error(item).startswith(error[:-1])
    else:
        assert only_error(item) == error


def test_seq_is_the_index_in_the_models_list() -> None:
    submission = parse_submission(
        {"market_view": "view", "proposals": [buy(symbol="XLE"), buy(action="hold"), buy(symbol="URA")]}
    )

    assert [(parsed.seq, parsed.proposal.symbol) for parsed in submission.proposals] == [
        (0, "XLE"),
        (2, "URA"),
    ]
    assert [malformed.error for malformed in submission.malformed] == ["action: 'hold' is not buy or sell"]


def test_raw_keeps_what_the_model_sent() -> None:
    item = buy(symbol=" xle ", target_pct="5", note="extra key")

    (parsed,) = parse_submission({"market_view": "view", "proposals": [item]}).proposals

    assert parsed.raw == item


def test_market_view_is_kept_only_when_it_is_a_string() -> None:
    assert parse_submission({"market_view": "Energy leads.", "proposals": []}).market_view == "Energy leads."
    assert parse_submission({"market_view": ["Energy"], "proposals": []}).market_view is None
    assert parse_submission({"proposals": []}).market_view is None


@pytest.mark.parametrize("proposals", ["none today", None, {"symbol": "XLE"}])
def test_proposals_that_are_not_a_list_become_one_malformed_row(proposals: object) -> None:
    tool_input = (
        {"market_view": "view"} if proposals is None else {"market_view": "view", "proposals": proposals}
    )

    submission = parse_submission(tool_input)

    assert submission.proposals == ()
    assert submission.malformed == (
        MalformedProposal(raw=tool_input, error=f"proposals: expected a list, got {proposals!r}"),
    )


def test_input_that_is_not_an_object_is_malformed() -> None:
    submission = parse_submission(["XLE"])

    assert submission.market_view is None
    assert submission.malformed == (
        MalformedProposal(raw=["XLE"], error="input: expected an object, got ['XLE']"),
    )


def test_empty_list_is_a_valid_answer() -> None:
    submission = parse_submission({"market_view": "Nothing clears the bar.", "proposals": []})

    assert (submission.proposals, submission.malformed) == ((), ())
