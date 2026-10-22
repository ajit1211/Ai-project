"""Anthropic adapter: the Claude Messages API through the official async SDK.

Translation rules (docs/architecture/ai-gateway.md#provider-adapters):

- `system` messages become the top-level `system` field; the rest keep their roles.
- Structured output uses the native `output_config.format` JSON-schema mode, with the schema
  passed through the SDK's `transform_schema` (which moves unsupported constraints into
  descriptions). The gateway validates the answer against the caller's original schema.
- `stop_reason: refusal` is a normal answer with finish reason `content_filter`;
  `max_tokens` and `model_context_window_exceeded` are `length`.
- Anthropic reports uncached input, cache reads and cache writes separately; NEXA's
  `input_tokens` is their sum.
- Sampling temperature is not sent: the Messages API for current Claude models has no
  temperature parameter, so the catalog marks these models `accepts_temperature = false`.
- Tools (Phase 10) become `tools` with `input_schema`; an assistant message's tool calls are
  `tool_use` blocks, and a `tool` message becomes a user turn of `tool_result` blocks. A
  streamed tool call arrives as `input_json_delta` fragments, joined and decoded at the end.
- The SDK's own retries are off: the gateway is the only place that retries.
"""

from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2
from anthropic import omit
from anthropic.types import MessageParam

from nexa_ai.errors import (
    AIAuthenticationError,
    AIContextTooLarge,
    AIError,
    AIInternalError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIProviderError,
    AIRateLimited,
    AITimeout,
    AIUnsupportedByProvider,
)
from nexa_ai.providers._http import request_timeout, retry_after_seconds, stream_timeout
from nexa_ai.providers.base import (
    ACTIVITY,
    ProviderCall,
    ProviderEvent,
    ProviderResult,
    StreamEnd,
    StreamStart,
    TextChunk,
    Timeouts,
    TokenCounts,
)
from nexa_ai.tools import decode_arguments, tool_call
from nexa_ai.types import FinishReason, Role, ToolCallPart
from nexa_common.logs import get_logger

log = get_logger("nexa.ai.anthropic")

_FINISH = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "pause_turn": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "model_context_window_exceeded": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
    "tool_use": FinishReason.TOOL_CALLS,
}
# Phrases in a 400 that mean "the parameter NEXA generated is not accepted by this model".
_UNSUPPORTED_HINTS = ("output_config", "schema", "temperature", "not supported", "unsupported")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        timeouts: Timeouts | None = None,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        timeouts = timeouts or Timeouts()
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=httpx2.Timeout(timeouts.request, connect=timeouts.connect),
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def chat(self, call: ProviderCall) -> ProviderResult:
        try:
            message = await self._client.messages.create(
                **self._params(call), timeout=request_timeout(call)
            )
        except Exception as exc:
            raise _map_error(exc, call.model) from exc
        content = "".join(block.text for block in message.content if block.type == "text")
        calls = tuple(
            tool_call(block.id, block.name, block.input)
            for block in message.content
            if block.type == "tool_use"
        )
        return ProviderResult(
            content=content,
            finish_reason=_finish(message.stop_reason),
            usage=_usage(message.usage),
            provider_response_id=message.id,
            tool_calls=calls,
        )

    async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        try:
            response = await self._client.messages.create(
                **self._params(call), stream=True, timeout=stream_timeout(call)
            )
        except Exception as exc:
            raise _map_error(exc, call.model) from exc

        stop_reason: str | None = None
        start_usage: Any = None
        output_tokens = 0
        # index -> (id, name, argument fragments) of the tool_use blocks seen so far
        tool_blocks: dict[int, tuple[str, str, list[str]]] = {}
        try:
            async with response:
                async for event in response:
                    if event.type == "message_start":
                        start_usage = event.message.usage
                        yield StreamStart(
                            provider_response_id=event.message.id,
                            input_tokens=_usage(start_usage).input_tokens,
                        )
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        if event.delta.text:
                            yield TextChunk(event.delta.text)
                    elif (
                        event.type == "content_block_start"
                        and event.content_block.type == "tool_use"
                    ):
                        block = event.content_block
                        tool_blocks[event.index] = (block.id, block.name, [])
                        yield ACTIVITY
                    elif (
                        event.type == "content_block_delta"
                        and event.delta.type == "input_json_delta"
                        and event.index in tool_blocks
                    ):
                        tool_blocks[event.index][2].append(event.delta.partial_json)
                        yield ACTIVITY
                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason or stop_reason
                        output_tokens = event.usage.output_tokens  # cumulative
                        yield ACTIVITY
                    elif event.type == "message_stop":
                        usage = _usage(start_usage) if start_usage is not None else None
                        yield StreamEnd(
                            finish_reason=_finish(stop_reason),
                            usage=TokenCounts(
                                input_tokens=usage.input_tokens,
                                output_tokens=output_tokens,
                                cached_input_tokens=usage.cached_input_tokens,
                                cache_write_input_tokens=usage.cache_write_input_tokens,
                            )
                            if usage is not None
                            else None,
                            tool_calls=tuple(
                                _assembled(*tool_blocks[index]) for index in sorted(tool_blocks)
                            ),
                        )
                        return
                    else:
                        # Thinking blocks, block boundaries, pings: alive, nothing to show.
                        yield ACTIVITY
        except AIError:
            raise
        except Exception as exc:
            raise _map_error(exc, call.model) from exc

    def _params(self, call: ProviderCall) -> dict[str, Any]:
        system = "\n\n".join(m.text for m in call.messages if m.role is Role.SYSTEM)
        messages: list[MessageParam] = []
        for message in call.messages:
            if message.role is Role.SYSTEM:
                continue
            if message.role is Role.TOOL:
                # Consecutive user turns are combined by the API, so the results stay first
                # in the turn that follows the tool_use blocks, as required.
                results: list[Any] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                    for result in message.tool_results
                ]
                messages.append({"role": "user", "content": results})
            elif message.role is Role.ASSISTANT:
                blocks: list[Any] = []
                if message.text or not message.tool_calls:
                    blocks.append({"type": "text", "text": message.text})
                blocks.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in message.tool_calls
                )
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append(
                    {"role": "user", "content": [{"type": "text", "text": message.text}]}
                )

        fmt = call.response_format
        output_config: Any = omit
        if fmt is not None:
            try:
                schema = anthropic.transform_schema(fmt.json_schema)
            except (ValueError, TypeError) as exc:
                # A valid JSON Schema this provider's structured outputs cannot express
                # (e.g. a property with no type). Another model may accept it.
                raise AIUnsupportedByProvider(
                    provider="anthropic", provider_model=call.model, provider_message=str(exc)
                ) from exc
            output_config = {"format": {"type": "json_schema", "schema": schema}}
        tools = call.request.tools or []
        return {
            "model": call.model,
            "max_tokens": call.max_output_tokens,
            "messages": messages,
            "system": system or omit,
            "stop_sequences": call.request.stop or omit,
            "metadata": {"user_id": call.end_user_id} if call.end_user_id else omit,
            "output_config": output_config,
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
            if tools
            else omit,
            "tool_choice": {"type": call.request.tool_choice} if tools else omit,
        }


def _assembled(call_id: str, name: str, fragments: list[str]) -> ToolCallPart:
    arguments, raw = decode_arguments("".join(fragments))
    return ToolCallPart(id=call_id, name=name, arguments=arguments, raw_arguments=raw)


def _finish(stop_reason: str | None) -> FinishReason:
    return _FINISH.get(stop_reason or "end_turn", FinishReason.STOP)


def _usage(usage: Any) -> TokenCounts:
    """Anthropic's `input_tokens` excludes cache reads and writes; NEXA's includes them."""
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    return TokenCounts(
        input_tokens=(usage.input_tokens or 0) + cache_read + cache_write,
        output_tokens=usage.output_tokens or 0,
        cached_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
    )


def _map_stream_error(kind: str, where: dict[str, Any]) -> AIError:
    if kind == "rate_limit_error":
        return AIRateLimited(**where)
    if kind == "timeout_error":
        return AITimeout(**where)
    if kind in ("authentication_error", "permission_error", "billing_error"):
        return AIAuthenticationError(**where)
    if kind == "invalid_request_error":
        return AIInvalidRequest(**where)
    return AIProviderError(**where)  # overloaded_error, api_error and anything new


def _map_error(exc: Exception, model: str) -> AIError:
    where: dict[str, Any] = {"provider": "anthropic", "provider_model": model}
    if isinstance(exc, AIError):
        return exc
    if isinstance(exc, anthropic.APITimeoutError):
        return AITimeout(**where)
    if isinstance(exc, anthropic.APIConnectionError):
        return AIProviderError(**where)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        kind = exc.type or ""
        text = (exc.message or "").lower()
        where |= {
            "provider_status": status,
            "provider_message": exc.message,
            "provider_request_id": exc.request_id,
        }
        if status < 400:
            # An `error` event inside a stream that started with HTTP 200: only the error
            # type says what happened.
            return _map_stream_error(kind, where)
        if status == 429:
            return AIRateLimited(
                retry_after_seconds=retry_after_seconds(exc.response.headers), **where
            )
        if status in (401, 402, 403) or kind in (
            "authentication_error",
            "permission_error",
            "billing_error",
        ):
            return AIAuthenticationError(**where)
        if status == 404:
            return AIModelUnavailable(**where)
        if status == 413:
            return AIContextTooLarge(**where)
        if status == 400:
            if "prompt is too long" in text or "context window" in text:
                return AIContextTooLarge(**where)
            if "credit balance" in text:
                return AIAuthenticationError(**where)
            if any(hint in text for hint in _UNSUPPORTED_HINTS):
                return AIUnsupportedByProvider(**where)
            return AIInvalidRequest(**where)
        if status in (408, 504):
            return AITimeout(**where)
        if status >= 500:
            return AIProviderError(**where)
        return AIInvalidRequest(**where)
    if isinstance(exc, anthropic.APIResponseValidationError | anthropic.APIError):
        return AIProviderError(**where)
    log.error("ai.anthropic.unmapped_error", error=type(exc).__name__)
    return AIInternalError(**where)
