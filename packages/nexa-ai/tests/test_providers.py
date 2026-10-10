"""The Anthropic and OpenAI adapters against canned HTTP responses.

The official SDKs run for real; only the network is replaced by an in-memory transport, so
these tests check request translation, response normalization and error mapping without
calling any provider.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from nexa_ai.errors import (
    AIAuthenticationError,
    AIContentRejected,
    AIContextTooLarge,
    AIError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIProviderError,
    AIRateLimited,
    AITimeout,
    AIUnsupportedByProvider,
)
from nexa_ai.providers._http import retry_after_seconds
from nexa_ai.providers.anthropic import AnthropicProvider
from nexa_ai.providers.base import (
    Activity,
    ProviderCall,
    ProviderEvent,
    StreamEnd,
    StreamStart,
    TextChunk,
    Timeouts,
)
from nexa_ai.providers.openai import OpenAIProvider
from nexa_ai.types import ChatRequest, FinishReason, Message, Role

pytestmark = pytest.mark.anyio

Handler = Callable[[httpx2.Request], httpx2.Response]

PERSON = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def call(
    *, model: str = "model-x", structured: bool = False, temperature: float | None = None
) -> ProviderCall:
    request = ChatRequest(
        messages=[
            Message(role=Role.SYSTEM, content="Be brief."),
            Message(role=Role.USER, content="Explain SSE."),
        ],
        response_format={"type": "json_schema", "name": "person", "schema": PERSON}
        if structured
        else None,
        stop=["END"],
    )
    return ProviderCall(
        request=request,
        model=model,
        max_output_tokens=256,
        timeouts=Timeouts(),
        temperature=temperature,
        end_user_id="opaque-user",
        request_id="req_1",
    )


class Recorder:
    """A transport handler that records requests and replies with queued responses."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def sse(*events: tuple[str | None, object]) -> httpx2.Response:
    lines = []
    for name, data in events:
        if name:
            lines.append(f"event: {name}")
        lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
        lines.append("")
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content="\n".join(lines) + "\n"
    )


def error(
    status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers or {})


async def collect(stream: Any) -> list[ProviderEvent]:
    return [event async for event in stream]


# --- Anthropic -----------------------------------------------------------------------------


def anthropic_with(recorder: Recorder) -> AnthropicProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))
    return AnthropicProvider(api_key="sk-ant-test-key", http_client=client)


def claude_message(**overrides: Any) -> dict[str, Any]:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [{"type": "text", "text": "Server-Sent Events stream text."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 20,
            "output_tokens": 7,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 30,
        },
    }
    message.update(overrides)
    return message


class TestAnthropic:
    async def test_request_translation(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=claude_message()))
        await anthropic_with(recorder).chat(call(temperature=0.2))

        sent = recorder.body()
        assert sent["model"] == "model-x"
        assert sent["system"] == "Be brief."
        assert sent["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Explain SSE."}]}
        ]
        assert sent["max_tokens"] == 256
        assert "temperature" not in sent  # the Messages API no longer takes one
        assert sent["stop_sequences"] == ["END"]
        assert sent["metadata"] == {"user_id": "opaque-user"}
        assert "output_config" not in sent
        assert recorder.requests[0].headers["x-api-key"] == "sk-ant-test-key"

    async def test_native_structured_output(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=claude_message()))
        await anthropic_with(recorder).chat(call(structured=True))
        fmt = recorder.body()["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["properties"] == PERSON["properties"]
        assert fmt["schema"]["additionalProperties"] is False
        assert "tool_choice" not in recorder.body()

    async def test_response_normalization_and_cache_tokens(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=claude_message()))
        result = await anthropic_with(recorder).chat(call())
        assert result.content == "Server-Sent Events stream text."
        assert result.finish_reason is FinishReason.STOP
        # Anthropic's input excludes cache reads and writes; NEXA's includes them.
        assert result.usage.input_tokens == 150
        assert result.usage.cached_input_tokens == 100
        assert result.usage.cache_write_input_tokens == 30
        assert result.usage.output_tokens == 7
        assert result.provider_response_id == "msg_1"

    @pytest.mark.parametrize(
        ("stop_reason", "finish"),
        [
            ("max_tokens", FinishReason.LENGTH),
            ("model_context_window_exceeded", FinishReason.LENGTH),
            ("refusal", FinishReason.CONTENT_FILTER),
            ("stop_sequence", FinishReason.STOP),
        ],
    )
    async def test_stop_reasons(self, stop_reason: str, finish: FinishReason) -> None:
        recorder = Recorder(httpx2.Response(200, json=claude_message(stop_reason=stop_reason)))
        assert (await anthropic_with(recorder).chat(call())).finish_reason is finish

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (
                error(
                    429,
                    {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
                    {"retry-after": "3"},
                ),
                AIRateLimited,
            ),
            (
                error(
                    401,
                    {
                        "type": "error",
                        "error": {"type": "authentication_error", "message": "bad key"},
                    },
                ),
                AIAuthenticationError,
            ),
            (
                error(402, {"type": "error", "error": {"type": "billing_error", "message": "pay"}}),
                AIAuthenticationError,
            ),
            (
                error(
                    400,
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Your credit balance is too low",
                        },
                    },
                ),
                AIAuthenticationError,
            ),
            (
                error(
                    404,
                    {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}},
                ),
                AIModelUnavailable,
            ),
            (
                error(
                    400,
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "prompt is too long: 208310 tokens > 200000 maximum",
                        },
                    },
                ),
                AIContextTooLarge,
            ),
            (
                error(
                    400,
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "output_config.format: unsupported keyword",
                        },
                    },
                ),
                AIUnsupportedByProvider,
            ),
            (
                error(
                    400,
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "messages: roles must alternate",
                        },
                    },
                ),
                AIInvalidRequest,
            ),
            (
                error(
                    529,
                    {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"},
                    },
                ),
                AIProviderError,
            ),
            (
                error(500, {"type": "error", "error": {"type": "api_error", "message": "oops"}}),
                AIProviderError,
            ),
            (
                error(
                    504, {"type": "error", "error": {"type": "timeout_error", "message": "late"}}
                ),
                AITimeout,
            ),
            (httpx2.ReadTimeout("slow"), AITimeout),
            (httpx2.ConnectError("refused"), AIProviderError),
        ],
    )
    async def test_error_mapping(
        self, response: httpx2.Response | Exception, expected: type[AIError]
    ) -> None:
        provider = anthropic_with(Recorder(response))
        with pytest.raises(expected) as caught:
            await provider.chat(call())
        error_ = caught.value
        assert error_.provider == "anthropic"
        # Provider text is kept for logs only; the client-facing message is NEXA's own.
        assert "Overloaded" not in error_.message
        assert "bad key" not in error_.message
        if isinstance(error_, AIRateLimited):
            assert error_.retry_after_seconds == 3
            assert error_.headers["Retry-After"] == "3"

    async def test_streaming(self) -> None:
        recorder = Recorder(
            sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            **claude_message(content=[], stop_reason=None),
                            "usage": {"input_tokens": 12, "output_tokens": 1},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                ("ping", {"type": "ping"}),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hello "},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "world"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 5},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            )
        )
        events = await collect(anthropic_with(recorder).stream(call()))
        assert recorder.body()["stream"] is True
        assert isinstance(events[0], StreamStart)
        assert events[0].input_tokens == 12
        assert [e.text for e in events if isinstance(e, TextChunk)] == ["Hello ", "world"]
        assert any(isinstance(e, Activity) for e in events)
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.finish_reason is FinishReason.STOP
        assert end.usage is not None
        assert (end.usage.input_tokens, end.usage.output_tokens) == (12, 5)

    async def test_streamed_refusal(self) -> None:
        recorder = Recorder(
            sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": claude_message(content=[], stop_reason=None),
                    },
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "refusal", "stop_sequence": None},
                        "usage": {"output_tokens": 0},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            )
        )
        events = await collect(anthropic_with(recorder).stream(call()))
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.finish_reason is FinishReason.CONTENT_FILTER

    async def test_error_event_mid_stream(self) -> None:
        recorder = Recorder(
            sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": claude_message(content=[], stop_reason=None),
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hel"},
                    },
                ),
                (
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"},
                    },
                ),
            )
        )
        received: list[ProviderEvent] = []

        async def consume() -> None:
            async for event in anthropic_with(recorder).stream(call()):
                received.append(event)

        with pytest.raises(AIProviderError):
            await consume()
        assert any(isinstance(e, TextChunk) for e in received)


# --- OpenAI --------------------------------------------------------------------------------


def openai_with(recorder: Recorder) -> OpenAIProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))
    return OpenAIProvider(api_key="sk-test-openai-key", http_client=client)


def completion(**message: Any) -> dict[str, Any]:
    finish = message.pop("finish_reason", "stop")
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Answer.", "refusal": None, **message},
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 9,
            "total_tokens": 49,
            "prompt_tokens_details": {"cached_tokens": 32},
        },
    }


def chunk(
    content: str | None = None, *, finish: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-x",
        "choices": []
        if usage
        else [
            {
                "index": 0,
                "delta": {"content": content} if content is not None else {},
                "finish_reason": finish,
            }
        ],
    }
    if usage:
        body["usage"] = usage
    return body


class TestOpenAI:
    async def test_request_translation(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=completion()))
        await openai_with(recorder).chat(call(structured=True, temperature=0.4))
        sent = recorder.body()
        assert sent["messages"] == [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Explain SSE."},
        ]
        assert sent["max_completion_tokens"] == 256
        assert "max_tokens" not in sent
        assert sent["temperature"] == 0.4
        assert sent["stop"] == ["END"]
        assert sent["safety_identifier"] == "opaque-user"
        assert sent["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "person", "schema": PERSON, "strict": False},
        }
        assert recorder.requests[0].headers["authorization"] == "Bearer sk-test-openai-key"

    async def test_response_normalization(self) -> None:
        result = await openai_with(Recorder(httpx2.Response(200, json=completion()))).chat(call())
        assert result.content == "Answer."
        assert result.finish_reason is FinishReason.STOP
        # OpenAI's prompt tokens already include cached tokens.
        assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (40, 32)
        assert result.usage.output_tokens == 9

    async def test_refusal(self) -> None:
        body = completion(content=None, refusal="I can't help with that.")
        result = await openai_with(Recorder(httpx2.Response(200, json=body))).chat(call())
        assert result.finish_reason is FinishReason.CONTENT_FILTER
        assert result.content == ""

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (
                error(
                    429,
                    {
                        "error": {
                            "type": "rate_limit_error",
                            "code": "rate_limit_exceeded",
                            "message": "slow",
                        }
                    },
                    {"retry-after-ms": "1500"},
                ),
                AIRateLimited,
            ),
            (
                error(
                    429,
                    {
                        "error": {
                            "type": "insufficient_quota",
                            "code": "insufficient_quota",
                            "message": "no credit",
                        }
                    },
                ),
                AIAuthenticationError,
            ),
            (
                error(
                    401,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "invalid_api_key",
                            "message": "bad key",
                        }
                    },
                ),
                AIAuthenticationError,
            ),
            (
                error(
                    404,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "model_not_found",
                            "message": "no model",
                        }
                    },
                ),
                AIModelUnavailable,
            ),
            (
                error(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "context_length_exceeded",
                            "message": "too long",
                        }
                    },
                ),
                AIContextTooLarge,
            ),
            (
                error(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "content_policy_violation",
                            "message": "no",
                        }
                    },
                ),
                AIContentRejected,
            ),
            (
                error(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "invalid_json_schema",
                            "message": "bad schema",
                        }
                    },
                ),
                AIUnsupportedByProvider,
            ),
            (
                error(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": None,
                            "message": "messages must not be empty",
                        }
                    },
                ),
                AIInvalidRequest,
            ),
            (
                error(
                    503,
                    {
                        "error": {
                            "type": "service_unavailable_error",
                            "code": "server_is_overloaded",
                            "message": "busy",
                        }
                    },
                ),
                AIProviderError,
            ),
            (httpx2.ReadTimeout("slow"), AITimeout),
        ],
    )
    async def test_error_mapping(
        self, response: httpx2.Response | Exception, expected: type[AIError]
    ) -> None:
        with pytest.raises(expected) as caught:
            await openai_with(Recorder(response)).chat(call())
        assert caught.value.provider == "openai"
        if isinstance(caught.value, AIRateLimited):
            assert caught.value.retry_after_seconds == 1.5

    async def test_streaming_with_usage(self) -> None:
        recorder = Recorder(
            sse(
                (None, chunk("")),
                (None, chunk("Hello ")),
                (None, chunk("world")),
                (None, chunk(finish="stop")),
                (
                    None,
                    chunk(usage={"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}),
                ),
                (None, "[DONE]"),
            )
        )
        events = await collect(openai_with(recorder).stream(call()))
        assert recorder.body()["stream_options"] == {"include_usage": True}
        assert isinstance(events[0], StreamStart)
        assert [e.text for e in events if isinstance(e, TextChunk)] == ["Hello ", "world"]
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.finish_reason is FinishReason.STOP
        assert end.usage is not None
        assert (end.usage.input_tokens, end.usage.output_tokens) == (11, 2)

    async def test_stream_without_usage_reports_none(self) -> None:
        recorder = Recorder(
            sse((None, chunk("Hi")), (None, chunk(finish="length")), (None, "[DONE]"))
        )
        events = await collect(openai_with(recorder).stream(call()))
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.usage is None
        assert end.finish_reason is FinishReason.LENGTH


class TestRetryAfter:
    def test_formats(self) -> None:
        assert retry_after_seconds({"retry-after-ms": "250"}) == 0.25
        assert retry_after_seconds({"retry-after": "7"}) == 7
        assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0
        assert retry_after_seconds({"retry-after": "soon"}) is None
        assert retry_after_seconds({}) is None
