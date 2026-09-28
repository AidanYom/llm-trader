from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock, Usage

from trader.scripted import SCRIPTED_USAGE, ScriptedClient, ScriptExhausted, reply, submit, text, tool_use


def test_responses_come_back_in_order_and_calls_are_recorded() -> None:
    first, second = reply(text("Looking.")), reply(text("Done."))
    client = ScriptedClient([first, second])
    messages: list[Any] = [{"role": "user", "content": "briefing"}]

    assert client.messages.create(model="m", messages=messages) is first
    messages.append({"role": "assistant", "content": "later"})
    assert client.messages.create(model="m", messages=messages) is second

    assert [call["model"] for call in client.calls] == ["m", "m"]
    assert len(client.calls[0]["messages"]) == 1  # a copy of the list as it was sent, not the live list
    assert len(client.calls[1]["messages"]) == 2


def test_running_out_of_responses_is_loud() -> None:
    client = ScriptedClient([reply(text("Only one."))])
    client.messages.create(messages=[])

    with pytest.raises(ScriptExhausted, match="no scripted response left for model call 2"):
        client.messages.create(messages=[])


def test_builders_make_sdk_messages() -> None:
    call = tool_use("get_news", {"symbol": "XLE"})
    message = reply(text("Checking energy."), call)

    assert isinstance(message.content[0], TextBlock)
    assert isinstance(message.content[1], ToolUseBlock)
    assert (call.name, call.input) == ("get_news", {"symbol": "XLE"})
    assert message.stop_reason == "tool_use"
    assert reply(text("No tools.")).stop_reason == "end_turn"
    assert reply(call, stop_reason="max_tokens").stop_reason == "max_tokens"
    assert message.usage == SCRIPTED_USAGE


def test_tool_use_ids_are_unique() -> None:
    assert tool_use("get_news", {}).id != tool_use("get_news", {}).id


def test_submit_builds_a_submit_proposals_call() -> None:
    call = submit("Energy leads.", [{"symbol": "XLE"}])

    assert call.name == "submit_proposals"
    assert call.input == {"market_view": "Energy leads.", "proposals": [{"symbol": "XLE"}]}


def test_usage_can_be_given() -> None:
    usage = Usage(input_tokens=5, output_tokens=6)

    assert reply(text("Hi."), usage=usage).usage is usage
