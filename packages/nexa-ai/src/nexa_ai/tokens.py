"""Cheap token estimates for pre-flight checks (context window, quota reservation) and for
usage the provider did not report (cancelled or broken streams).

Real counts come from the provider with every complete response. The estimate errs high on
purpose: three characters per token over-counts English prose by about a third, and comes
close for code and non-Latin scripts, which tokenize less efficiently. Over-estimating only
reserves a little more quota for a moment; under-estimating would let requests through that
the provider then rejects. No tokenizer library is used, so `nexa_ai` stays provider-neutral.
"""

import json
from collections.abc import Iterable

from nexa_ai.types import Message

CHARS_PER_TOKEN = 3
_PER_MESSAGE_OVERHEAD = 4
_PER_TOOL_PART_OVERHEAD = 8


def estimate_text_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_message_tokens(messages: Iterable[Message]) -> int:
    return sum(_message_tokens(m) for m in messages)


def _message_tokens(message: Message) -> int:
    tokens = estimate_text_tokens(message.text) + _PER_MESSAGE_OVERHEAD
    for call in message.tool_calls:
        arguments = call.raw_arguments or json.dumps(call.arguments)
        tokens += estimate_text_tokens(call.name + arguments) + _PER_TOOL_PART_OVERHEAD
    for result in message.tool_results:
        tokens += estimate_text_tokens(result.content) + _PER_TOOL_PART_OVERHEAD
    return tokens
