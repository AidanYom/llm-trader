from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, Usage

from trader.agent import (
    BUDGET_EXHAUSTED,
    NUDGE,
    TOOLS,
    AgentResult,
    ModelClient,
    UsageMeter,
    anthropic_client,
    run_agent,
)
from trader.brokers.base import BrokerError
from trader.brokers.fake import FakeBroker
from trader.models import Bar, Prices, Strategy
from trader.scripted import ScriptedClient, ScriptExhausted, reply, submit, text, tool_use

NOW = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # Monday, 08:31 in New York
SYSTEM_PROMPT = "You are the research and decision agent."
BRIEFING = "# Daily briefing: 2026-09-28 (pre-market, US/Eastern)\n\n## Account\n"
STRATEGY = Strategy(
    model="claude-sonnet-5",
    max_tokens=4000,
    max_turns=15,
    max_tool_calls=12,
    benchmark="SPY",
    sector_etfs=("XLK", "XLE"),
    industry_etfs=("SMH",),
    baseline_basket=("XLK", "XLE"),
    news_lookback_hours=24,
    max_news_items=40,
    price=Prices(input=2.0, output=10.0, cache_write=2.5, cache_read=0.2),
    system_frame="config/system_frame.md",
    strategy_prompt="config/strategy.md",
)
XLE_BUY = {
    "symbol": "XLE",
    "action": "buy",
    "target_pct": 5,
    "stop_pct": 8,
    "thesis": "Energy leads on 1m relative strength after the OPEC+ cuts.",
    "invalidation": "XLE closes below its 20-day low.",
    "confidence": 0.6,
}


class BrokenBroker(FakeBroker):
    def get_daily_bars(self, symbols: Sequence[str], sessions: int) -> dict[str, list[Bar]]:
        raise BrokerError("the data feed is down")


def run(
    *responses: Message, strategy: Strategy = STRATEGY, broker: FakeBroker | None = None
) -> tuple[AgentResult, ScriptedClient, FakeBroker]:
    client = ScriptedClient(responses)
    broker = FakeBroker(now=NOW) if broker is None else broker
    result = run_agent(
        client,
        strategy=strategy,
        system_prompt=SYSTEM_PROMPT,
        briefing=BRIEFING,
        broker=broker,
        now=NOW,
        meter=UsageMeter(strategy.price),
    )
    return result, client, broker


def sent_results(client: ScriptedClient, call: int) -> list[Any]:
    """The tool_result blocks that model call `call` (0-based) sent back, from its last user message."""
    last = client.calls[call]["messages"][-1]
    assert last["role"] == "user"
    content: list[Any] = last["content"]
    return content


def test_research_then_submit() -> None:
    price_call = tool_use("get_price_history", {"symbol": "XLE"})
    news_call = tool_use("get_news", {"symbol": "XLE"})

    result, client, _ = run(
        reply(text("Checking energy."), price_call, news_call), reply(submit("Energy leads.", [XLE_BUY]))
    )

    assert result.submitted and result.turns == 2
    assert result.submission.market_view == "Energy leads."
    assert [parsed.proposal.symbol for parsed in result.submission.proposals] == ["XLE"]
    assert set(result.stats) == {"XLE"}  # get_price_history's stats go to the risk engine
    assert result.researched == {"XLE"}
    # Both results go back in one user message, in order, answering each call.
    price, news = sent_results(client, 1)
    assert price["tool_use_id"] == price_call.id and price["content"].startswith("XLE: last close")
    assert news["tool_use_id"] == news_call.id
    assert news["content"].startswith("Untrusted third-party text:")
    assert "is_error" not in price and "is_error" not in news
    assert [(call.seq, call.name) for call in result.tool_calls] == [
        (0, "get_price_history"),
        (1, "get_news"),
        (2, "submit_proposals"),
    ]
    assert result.tool_calls[0].result == price["content"]
    assert result.tool_calls[2].result is None  # nothing went back to the model


def test_requests_cache_the_system_prompt_and_the_briefing() -> None:
    _, client, _ = run(reply(tool_use("get_news", {"symbol": "XLE"})), reply(submit("view", [])))

    for call in client.calls:
        assert call["model"] == "claude-sonnet-5"
        assert call["max_tokens"] == 4000
        assert call["thinking"] == {"type": "disabled"}  # Sonnet 5 would otherwise think (HANDOFF §5)
        assert call["system"] == [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
        assert call["messages"][0] == {
            "role": "user",
            "content": [{"type": "text", "text": BRIEFING, "cache_control": {"type": "ephemeral"}}],
        }


def test_the_only_tools_offered_are_the_three_read_only_ones() -> None:
    _, client, _ = run(reply(tool_use("get_news", {"symbol": "XLE"})), reply(submit("view", [])))

    for call in client.calls:
        assert call["tools"] is TOOLS
        assert [tool["name"] for tool in call["tools"]] == [
            "get_price_history",
            "get_news",
            "submit_proposals",
        ]


def test_the_assistant_turn_is_appended_unchanged() -> None:
    first = reply(text("Checking."), tool_use("get_news", {"symbol": "XLE"}))

    _, client, _ = run(first, reply(submit("view", [])))

    assert client.calls[1]["messages"][1] == {"role": "assistant", "content": first.content}


def test_a_nudge_that_recovers() -> None:
    result, client, _ = run(reply(text("Energy looks strong.")), reply(submit("view", [])))

    assert result.submitted and result.turns == 2
    assert client.calls[1]["messages"][-1] == {"role": "user", "content": NUDGE}


def test_a_nudge_that_gives_up() -> None:
    result, client, _ = run(reply(text("Energy looks strong.")), reply(text("I'd rather not decide.")))

    assert not result.submitted
    assert result.turns == len(client.calls) == 2
    assert result.submission.market_view is None and result.submission.proposals == ()


def test_only_tool_less_responses_in_a_row_end_the_run() -> None:
    result, _, _ = run(
        reply(text("Thinking.")),
        reply(tool_use("get_news", {"symbol": "XLE"})),
        reply(text("Still thinking.")),
        reply(submit("view", [])),
    )

    assert result.submitted and result.turns == 4


def test_the_research_budget_is_enforced() -> None:
    strategy = replace(STRATEGY, max_tool_calls=2)

    result, client, broker = run(
        reply(*(tool_use("get_news", {"symbol": symbol}) for symbol in ("XLE", "SMH", "URA"))),
        reply(tool_use("get_price_history", {"symbol": "XLE"})),
        reply(submit("view", [])),
        strategy=strategy,
    )

    assert [result["content"] for result in sent_results(client, 1)][2] == BUDGET_EXHAUSTED
    assert [result["content"] for result in sent_results(client, 2)] == [BUDGET_EXHAUSTED]
    assert broker.calls == ["get_news", "get_news"]  # the calls over budget never ran
    assert result.submitted


def test_invalid_symbols_and_errors_count_toward_the_budget() -> None:
    result, client, _ = run(
        reply(tool_use("get_news", {"symbol": "BRK B"}), tool_use("get_news", {"symbol": "XLE"})),
        reply(submit("view", [])),
        strategy=replace(STRATEGY, max_tool_calls=1),
    )

    invalid, second = sent_results(client, 1)
    assert (invalid["content"], invalid["is_error"]) == ("Invalid symbol.", True)
    assert second["content"] == BUDGET_EXHAUSTED
    assert result.submitted


def test_submit_ends_the_loop_and_ignores_other_calls() -> None:
    result, client, broker = run(reply(tool_use("get_news", {"symbol": "XLE"}), submit("view", [XLE_BUY])))

    assert result.submitted and len(client.calls) == 1
    assert broker.calls == []  # the research call beside submit never ran
    assert [(call.name, call.result) for call in result.tool_calls] == [
        ("get_news", None),
        ("submit_proposals", None),
    ]


def test_the_first_of_two_submits_is_used() -> None:
    result, _, _ = run(reply(submit("first", []), submit("second", [XLE_BUY])))

    assert result.submission.market_view == "first"
    assert result.submission.proposals == ()
    assert len(result.tool_calls) == 2


def test_the_turn_cap_ends_the_run() -> None:
    result, client, broker = run(
        reply(tool_use("get_news", {"symbol": "XLE"})),
        reply(tool_use("get_news", {"symbol": "SMH"})),
        strategy=replace(STRATEGY, max_turns=2),
    )

    assert not result.submitted and len(client.calls) == 2
    assert broker.calls == ["get_news"]  # the last turn's call never ran
    assert result.tool_calls[1].result is None


def test_a_tool_less_response_on_the_last_turn_is_not_nudged() -> None:
    result, client, _ = run(
        reply(tool_use("get_news", {"symbol": "XLE"})),
        reply(text("Out of turns.")),
        strategy=replace(STRATEGY, max_turns=2),
    )

    assert not result.submitted and len(client.calls) == 2


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_calls_in_a_cut_off_response_never_run(stop_reason: Any) -> None:
    news_call = tool_use("get_news", {"symbol": "XLE"})
    cut_off = reply(news_call, submit("partial", [XLE_BUY]), stop_reason=stop_reason)

    result, client, broker = run(cut_off, reply(submit("complete", [])))

    assert broker.calls == []
    news, partial_submit = sent_results(client, 1)
    expected = f"This response was cut off ({stop_reason}), so the call did not run. Call it again."
    assert news == {
        "type": "tool_result",
        "tool_use_id": news_call.id,
        "content": expected,
        "is_error": True,
    }
    assert partial_submit["content"] == expected
    assert result.submission.market_view == "complete"  # the cut-off submit wasn't accepted
    assert [call.result for call in result.tool_calls] == [expected, expected, None]


def test_cut_off_calls_do_not_use_the_research_budget() -> None:
    result, client, _ = run(
        reply(tool_use("get_news", {"symbol": "XLE"}), stop_reason="max_tokens"),
        reply(tool_use("get_news", {"symbol": "XLE"})),
        reply(submit("view", [])),
        strategy=replace(STRATEGY, max_tool_calls=1),
    )

    assert sent_results(client, 2)[0]["content"].startswith("Untrusted third-party text:")
    assert result.submitted


def test_tool_errors_go_back_to_the_model_and_the_loop_goes_on() -> None:
    result, client, _ = run(
        reply(tool_use("get_price_history", {"symbol": "XLE"})),
        reply(submit("view", [])),
        broker=BrokenBroker(now=NOW),
    )

    (error,) = sent_results(client, 1)
    assert (error["content"], error["is_error"]) == ("Tool error: BrokerError: the data feed is down", True)
    assert result.submitted and result.stats == {}


def test_usage_is_summed_across_turns_and_priced() -> None:
    meter = UsageMeter(STRATEGY.price)
    client = ScriptedClient(
        [
            reply(
                tool_use("get_news", {"symbol": "XLE"}),
                usage=Usage(input_tokens=1_000, output_tokens=200, cache_creation_input_tokens=3_000),
            ),
            reply(
                submit("view", []),
                usage=Usage(input_tokens=500, output_tokens=100, cache_read_input_tokens=3_000),
            ),
        ]
    )

    run_agent(
        client,
        strategy=STRATEGY,
        system_prompt=SYSTEM_PROMPT,
        briefing=BRIEFING,
        broker=FakeBroker(now=NOW),
        now=NOW,
        meter=meter,
    )

    usage = meter.usage
    assert (usage.input_tokens, usage.output_tokens, usage.cache_write_tokens, usage.cache_read_tokens) == (
        1_500,
        300,
        3_000,
        3_000,
    )
    # (1,500 × $2 + 300 × $10 + 3,000 × $2.50 + 3,000 × $0.20) per million tokens
    assert usage.cost_usd == pytest.approx(0.0141)


def test_usage_is_kept_when_a_later_turn_fails() -> None:
    meter = UsageMeter(STRATEGY.price)
    client = ScriptedClient(
        [reply(tool_use("get_news", {"symbol": "XLE"}), usage=Usage(input_tokens=7, output_tokens=3))]
    )

    with pytest.raises(ScriptExhausted):
        run_agent(
            client,
            strategy=STRATEGY,
            system_prompt=SYSTEM_PROMPT,
            briefing=BRIEFING,
            broker=FakeBroker(now=NOW),
            now=NOW,
            meter=meter,
        )

    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (7, 3)


def test_the_real_client_has_the_shape_the_loop_needs() -> None:
    # mypy checks the assignment: anthropic.Anthropic must satisfy the ModelClient protocol. Building a
    # client makes no network call.
    client: ModelClient = anthropic.Anthropic(api_key="test-key-not-real")

    assert callable(client.messages.create)


def test_the_real_client_waits_two_minutes_and_retries_twice() -> None:
    # A 4,000-token turn can take over a minute (HANDOFF §5). Building the client makes no network call.
    client = anthropic_client("test-key-not-real")
    loop_client: ModelClient = client

    assert (client.timeout, client.max_retries) == (120.0, 2)
    assert callable(loop_client.messages.create)
