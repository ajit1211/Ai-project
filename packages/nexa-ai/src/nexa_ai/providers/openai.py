"""OpenAI adapter: Chat Completions through the official async SDK.

Translation rules (docs/architecture/ai-gateway.md#provider-adapters):

- Messages keep their roles; `max_output_tokens` is sent as `max_completion_tokens`.
- Structured output uses `response_format: json_schema` with `strict: false`. Strict mode
  rejects common schemas (optional properties, missing `additionalProperties: false`), and
  the gateway validates every answer itself and repairs once, so strictness adds no safety.
- A model refusal (`message.refusal`) or a content-filter stop is finish reason
  `content_filter` with empty content.
- Streams request `include_usage`, so the final chunk carries token counts.
- OpenAI's prompt tokens already include cached tokens, which matches NEXA's `input_tokens`.
- Tools (Phase 10) become `function` tools. An assistant message's calls are `tool_calls` with
  JSON-encoded arguments; a `tool` message becomes one `tool` message per result. A streamed
  call arrives in fragments keyed by `index`, joined and decoded at the end.
- The SDK's own retries are off: the gateway is the only place that retries.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import openai
from openai import omit
from openai.types.chat import ChatCompletionMessageParam

from nexa_ai.errors import (
    AIAuthenticationError,
    AIContentRejected,
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
from nexa_ai.tools import call_arguments_text, decode_arguments
from nexa_ai.types import FinishReason, Role, ToolCallPart
from nexa_common.logs import get_logger

log = get_logger("nexa.ai.openai")

_FINISH = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
}
# 429s that are account problems, not congestion: waiting will not help.
_ACCOUNT_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
_UNSUPPORTED_CODES = frozenset(
    {"invalid_json_schema", "unsupported_parameter", "unsupported_value"}
)
_CONTENT_CODES = frozenset({"content_policy_violation", "content_filter"})


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        timeouts: Timeouts | None = None,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        timeouts = timeouts or Timeouts()
        self._client = openai.AsyncOpenAI(
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
            completion = await self._client.chat.completions.create(
                **self._params(call), timeout=request_timeout(call)
            )
        except Exception as exc:
            raise _map_error(exc, call.model) from exc
        if not completion.choices:
            raise AIProviderError("The provider returned no answer.", provider=self.name)
        choice = completion.choices[0]
        refused = bool(getattr(choice.message, "refusal", None))
        calls = tuple(
            _call(index, tc.id, tc.function.name, tc.function.arguments)
            for index, tc in enumerate(getattr(choice.message, "tool_calls", None) or [])
            if getattr(tc, "type", "function") == "function"
        )
        return ProviderResult(
            content="" if refused else (choice.message.content or ""),
            finish_reason=FinishReason.CONTENT_FILTER if refused else _finish(choice.finish_reason),
            usage=_usage(completion.usage),
            provider_response_id=completion.id,
            tool_calls=() if refused else calls,
        )

    async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        try:
            response = await self._client.chat.completions.create(
                **self._params(call),
                stream=True,
                stream_options={"include_usage": True},
                timeout=stream_timeout(call),
            )
        except Exception as exc:
            raise _map_error(exc, call.model) from exc

        started = False
        finish: str | None = None
        refused = False
        usage: TokenCounts | None = None
        # index -> [id, name, argument fragments] of the tool calls seen so far
        calls: dict[int, tuple[list[str], list[str], list[str]]] = {}
        try:
            async with response:
                async for chunk in response:
                    if not started:
                        started = True
                        yield StreamStart(provider_response_id=chunk.id)
                    if chunk.usage is not None:
                        usage = _usage(chunk.usage)
                    if not chunk.choices:
                        yield ACTIVITY
                        continue
                    choice = chunk.choices[0]
                    finish = choice.finish_reason or finish
                    if getattr(choice.delta, "refusal", None):
                        refused = True
                    for fragment in getattr(choice.delta, "tool_calls", None) or []:
                        ids, names, arguments = calls.setdefault(fragment.index, ([], [], []))
                        if fragment.id:
                            ids.append(fragment.id)
                        function = fragment.function
                        if function is not None:
                            if function.name:
                                names.append(function.name)
                            if function.arguments:
                                arguments.append(function.arguments)
                    if choice.delta.content and not refused:
                        yield TextChunk(choice.delta.content)
                    else:
                        yield ACTIVITY
        except AIError:
            raise
        except Exception as exc:
            raise _map_error(exc, call.model) from exc
        yield StreamEnd(
            finish_reason=FinishReason.CONTENT_FILTER if refused else _finish(finish),
            usage=usage,
            tool_calls=()
            if refused
            else tuple(
                _call(index, ids[0] if ids else None, "".join(names), "".join(arguments))
                for index, (ids, names, arguments) in sorted(calls.items())
            ),
        )

    def _params(self, call: ProviderCall) -> dict[str, Any]:
        messages: list[ChatCompletionMessageParam] = []
        for message in call.messages:
            if message.role is Role.SYSTEM:
                messages.append({"role": "system", "content": message.text})
            elif message.role is Role.TOOL:
                messages.extend(
                    {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                    for r in message.tool_results
                )
            elif message.role is Role.ASSISTANT:
                if message.tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.text or None,
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.name,
                                        "arguments": call_arguments_text(c),
                                    },
                                }
                                for c in message.tool_calls
                            ],
                        }
                    )
                else:
                    messages.append({"role": "assistant", "content": message.text})
            else:
                messages.append({"role": "user", "content": message.text})
        fmt = call.response_format
        tools = call.request.tools or []
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            if tools
            else omit,
            "tool_choice": call.request.tool_choice if tools else omit,
            "model": call.model,
            "messages": messages,
            "max_completion_tokens": call.max_output_tokens,
            "temperature": call.temperature if call.temperature is not None else omit,
            "stop": call.request.stop or omit,
            "safety_identifier": call.end_user_id or omit,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": fmt.name, "schema": fmt.json_schema, "strict": False},
            }
            if fmt is not None
            else omit,
        }


def _call(index: int, call_id: str | None, name: str | None, arguments: str | None) -> ToolCallPart:
    # Some OpenAI-compatible servers omit the id; the result must still be able to answer it.
    decoded, raw = decode_arguments(arguments)
    return ToolCallPart(
        id=(call_id or f"call_{index}")[:128],
        name=(name or "unknown")[:128],
        arguments=decoded,
        raw_arguments=raw,
    )


def _finish(reason: str | None) -> FinishReason:
    return _FINISH.get(reason or "stop", FinishReason.STOP)


def _usage(usage: Any) -> TokenCounts:
    if usage is None:
        return TokenCounts()
    details = getattr(usage, "prompt_tokens_details", None)
    return TokenCounts(
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        cached_input_tokens=(getattr(details, "cached_tokens", None) or 0) if details else 0,
        cache_write_input_tokens=(getattr(details, "cache_write_tokens", None) or 0)
        if details
        else 0,
    )


def _map_error(exc: Exception, model: str) -> AIError:
    where: dict[str, Any] = {"provider": "openai", "provider_model": model}
    if isinstance(exc, AIError):
        return exc
    if isinstance(exc, openai.APITimeoutError):
        return AITimeout(**where)
    if isinstance(exc, openai.APIConnectionError):
        return AIProviderError(**where)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        code = str(exc.code or "")
        kind = str(exc.type or "")
        text = (exc.message or "").lower()
        where |= {
            "provider_status": status,
            "provider_message": exc.message,
            "provider_request_id": exc.request_id,
        }
        if status == 429:
            if code in _ACCOUNT_CODES or kind in _ACCOUNT_CODES:
                return AIAuthenticationError(**where)
            return AIRateLimited(
                retry_after_seconds=retry_after_seconds(exc.response.headers), **where
            )
        if status in (401, 403):
            return AIAuthenticationError(**where)
        if status == 404:
            return AIModelUnavailable(**where)
        if status == 400:
            if (
                code == "context_length_exceeded"
                or "context window" in text
                or "maximum context length" in text
            ):
                return AIContextTooLarge(**where)
            if code in _CONTENT_CODES:
                return AIContentRejected(**where)
            if code in _UNSUPPORTED_CODES or any(
                hint in text for hint in ("response_format", "schema", "temperature")
            ):
                return AIUnsupportedByProvider(**where)
            return AIInvalidRequest(**where)
        if status in (408, 504):
            return AITimeout(**where)
        if status >= 500:
            return AIProviderError(**where)
        return AIInvalidRequest(**where)
    if isinstance(exc, openai.APIError):
        return AIProviderError(**where)
    log.error("ai.openai.unmapped_error", error=type(exc).__name__)
    return AIInternalError(**where)
