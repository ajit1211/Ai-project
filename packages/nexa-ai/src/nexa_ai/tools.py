"""Tool calling, provider-neutral (docs/architecture/tools.md#gateway).

The gateway carries tool definitions to the model and the model's tool calls back; it never
runs a tool. This module holds what every adapter and the gateway share:

- `check_tool_request`: the definitions are valid (names, JSON Schemas of arguments) and the
  history is well formed: tool calls only in assistant messages, every `tool` message answers
  exactly the calls of the assistant message before it, call ids are unique, and only offered
  tools appear. A malformed history is the caller's bug and never reaches a provider.
- `decode_arguments`: the model's argument text as a JSON object, or the reason it is not one.
  Arguments are model output: bounded in size and depth, never trusted.
"""

import json
from collections.abc import Sequence
from typing import Any

from nexa_ai.errors import AIInvalidRequest
from nexa_ai.structured import check_schema
from nexa_ai.tokens import estimate_text_tokens
from nexa_ai.types import (
    MAX_TOOL_ARGUMENTS_CHARS,
    ChatRequest,
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)

MAX_ARGUMENT_DEPTH = 16


def check_tool_request(request: ChatRequest) -> None:
    """Raise AIInvalidRequest for invalid tool definitions or a malformed tool history."""
    offered = {tool.name for tool in request.tools or []}
    for tool in request.tools or []:
        try:
            check_schema(tool.input_schema)
        except AIInvalidRequest as exc:
            reason = exc.message.replace("The response schema", "Its input schema")
            raise AIInvalidRequest(f"Tool '{tool.name}': {reason}") from exc
    if request.tool_choice != "auto" and not offered:
        raise AIInvalidRequest("tool_choice needs tools.")
    _check_history(request.messages)


def _check_history(messages: Sequence[Message]) -> None:
    seen_ids: set[str] = set()
    open_calls: list[str] | None = None  # ids of the last assistant message's calls, unanswered
    for message in messages:
        calls = message.tool_calls
        results = message.tool_results
        if message.role is Role.TOOL:
            if open_calls is None:
                raise AIInvalidRequest(
                    "A tool message must follow an assistant message with tool calls."
                )
            if any(not isinstance(part, ToolResultPart) for part in message.parts):
                raise AIInvalidRequest("A tool message may only contain tool results.")
            answered = [result.call_id for result in results]
            if sorted(answered) != sorted(open_calls):
                raise AIInvalidRequest(
                    "A tool message must answer every call of the message before it, once."
                )
            open_calls = None
            continue
        if open_calls is not None:
            raise AIInvalidRequest("Tool calls must be answered before the conversation goes on.")
        if results:
            raise AIInvalidRequest("Tool results belong in tool messages.")
        if calls:
            if message.role is not Role.ASSISTANT:
                raise AIInvalidRequest("Only assistant messages can contain tool calls.")
            if any(not isinstance(part, TextPart | ToolCallPart) for part in message.parts):
                raise AIInvalidRequest("An assistant message may contain text and tool calls.")
            # A call of a tool that was not offered may be in the history: the model made it
            # up, and the caller answered it with an error result.
            for call in calls:
                if call.id in seen_ids:
                    raise AIInvalidRequest("Tool call ids must be unique.")
                seen_ids.add(call.id)
            open_calls = [call.id for call in calls]
    if open_calls is not None:
        raise AIInvalidRequest("The last tool calls have no results.")


def decode_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """(arguments, None) for a JSON object; ({}, the raw text, bounded) for anything else.

    An empty string means no arguments, which some providers send for a tool without
    parameters."""
    text = (raw or "").strip()
    if not text:
        return {}, None
    if len(text) > MAX_TOOL_ARGUMENTS_CHARS:
        return {}, text[:MAX_TOOL_ARGUMENTS_CHARS]
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return {}, text
    if not isinstance(value, dict) or _depth(value) > MAX_ARGUMENT_DEPTH:
        return {}, text
    return value, None


def tool_call(call_id: str, name: str, arguments: Any) -> ToolCallPart:
    """A ToolCallPart from a provider's already-decoded arguments (Anthropic sends an object)."""
    if isinstance(arguments, dict) and _depth(arguments) <= MAX_ARGUMENT_DEPTH:
        encoded = json.dumps(arguments)
        if len(encoded) <= MAX_TOOL_ARGUMENTS_CHARS:
            return ToolCallPart(id=call_id, name=name, arguments=arguments)
        return ToolCallPart(id=call_id, name=name, raw_arguments=encoded[:MAX_TOOL_ARGUMENTS_CHARS])
    return ToolCallPart(id=call_id, name=name, raw_arguments=json.dumps(arguments)[:1000])


def call_arguments_text(call: ToolCallPart) -> str:
    """The arguments as the model produced them, to send the call back in the history."""
    if call.raw_arguments is not None:
        return call.raw_arguments
    return json.dumps(call.arguments, ensure_ascii=False)


def estimate_tools_tokens(tools: Sequence[ToolSpec] | None) -> int:
    """Tool definitions count against the context window like any other input."""
    return sum(
        estimate_text_tokens(tool.name + tool.description + json.dumps(tool.input_schema)) + 8
        for tool in tools or ()
    )


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_ARGUMENT_DEPTH:
        return level
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level
