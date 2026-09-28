"""ScriptedClient: a stand-in for the Anthropic client that returns prepared responses (HANDOFF §14).

Tests and offline mode use it in place of `anthropic.Anthropic`. Its responses are real
`anthropic.types.Message` objects, so the agent loop runs exactly as it would against Claude, but nothing
here calls the API: agent.py remains the only module that does. The builders below make the responses.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from anthropic.types import ContentBlock, Message, StopReason, TextBlock, ToolUseBlock, Usage

SCRIPTED_USAGE = Usage(
    input_tokens=1_000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0
)

_ids = itertools.count(1)


class ScriptExhausted(RuntimeError):
    """The loop asked for more responses than the script holds: a test or scenario is missing one."""


class ScriptedClient:
    """Answers each `messages.create(**kwargs)` with the next prepared response, and records every call."""

    def __init__(self, responses: Iterable[Message]) -> None:
        self._responses = deque(responses)
        self.calls: list[dict[str, Any]] = []  # each call's arguments, as they were when it was made
        self.messages = _Messages(self)

    def _next(self, kwargs: dict[str, Any]) -> Message:
        # The loop keeps appending to its messages list, so keep a copy of the list as it was sent.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", ()))})
        if not self._responses:
            raise ScriptExhausted(f"no scripted response left for model call {len(self.calls)}")
        return self._responses.popleft()


class _Messages:
    def __init__(self, client: ScriptedClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Message:
        return self._client._next(kwargs)


def reply(
    *blocks: ContentBlock, stop_reason: StopReason | None = None, usage: Usage | None = None
) -> Message:
    """A model response. Its stop reason is tool_use when a block is a tool call, otherwise end_turn."""
    if stop_reason is None:
        stop_reason = "tool_use" if any(isinstance(block, ToolUseBlock) for block in blocks) else "end_turn"
    return Message(
        id=f"msg_scripted_{next(_ids)}",
        type="message",
        role="assistant",
        model="scripted",
        content=list(blocks),
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=SCRIPTED_USAGE if usage is None else usage,
    )


def text(words: str) -> TextBlock:
    return TextBlock(type="text", text=words)


def tool_use(name: str, tool_input: Mapping[str, object]) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=f"toolu_scripted_{next(_ids)}", name=name, input=dict(tool_input))


def submit(market_view: object, proposals: object) -> ToolUseBlock:
    """A `submit_proposals` call. The arguments are untyped so tests can send what a model might."""
    return tool_use("submit_proposals", {"market_view": market_view, "proposals": proposals})
