"""Tool calling through the gateway (Phase 10): definitions and history are validated before
any provider is called, only tool-capable models are chosen, tool calls come back normalized
on both paths, and the Anthropic and OpenAI adapters translate tools both ways.

The gateway never executes a tool; Runtime's executor does (apps/runtime/tests/test_tools_*).
"""

import json
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIInvalidRequest, AIModelUnavailable
from nexa_ai.gateway import MemoryUsageSink
from nexa_ai.providers.base import ProviderCall, StreamEnd, Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.registry import Alias, ModelSpec
from nexa_ai.testing import ALL_PHASE4, PRIMARY, StaticCatalog, make_registry
from nexa_ai.tokens import estimate_message_tokens
from nexa_ai.tools import decode_arguments, estimate_tools_tokens
from nexa_ai.types import (
    Capability,
    ChatRequest,
    FinishReason,
    Message,
    MessageEnd,
    MessageStart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    UsageEvent,
)
from tests.conftest import GatewayFactory
from tests.test_providers import (
    Recorder,
    anthropic_with,
    chunk,
    claude_message,
    collect,
    completion,
    openai_with,
    sse,
)

pytestmark = pytest.mark.anyio

CALCULATOR = ToolSpec(
    name="calculator",
    description="Evaluate an arithmetic expression.",
    input_schema={
        "type": "object",
        "properties": {"expression": {"type": "string", "maxLength": 200}},
        "required": ["expression"],
        "additionalProperties": False,
    },
)
CLOCK = ToolSpec(
    name="current_time",
    description="The current UTC time.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
TOOLS_MODEL = ModelSpec(
    id=uuid4(),
    provider="mock",
    provider_model="mock-tools",
    display_name="Mock with tools",
    capabilities=ALL_PHASE4 | {Capability.TOOL_CALLING},
    context_window=16_000,
    max_output_tokens=2_000,
)


@pytest.fixture
def tool_gateway(make_gateway: GatewayFactory) -> Any:
    registry = make_registry(
        Alias(name="nexa-tools", display_name="Tools", is_default=True, targets=(TOOLS_MODEL,)),
        Alias(name="nexa-plain", display_name="Plain", targets=(PRIMARY,)),
    )
    return make_gateway(catalog=StaticCatalog(registry))


def user(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def calls(*parts: ToolCallPart, text: str = "") -> Message:
    content: list[Any] = [TextPart(text=text)] if text else []
    return Message(role=Role.ASSISTANT, content=[*content, *parts])


def results(*parts: ToolResultPart) -> Message:
    return Message(role=Role.TOOL, content=list(parts))


ADD = ToolCallPart(id="call_1", name="calculator", arguments={"expression": "2+2"})
ADD_RESULT = ToolResultPart(call_id="call_1", content=json.dumps({"result_text": "4"}))


class TestValidation:
    def test_tool_names_are_unique(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(messages=[user("hi")], tools=[CALCULATOR, CALCULATOR])

    @pytest.mark.parametrize(
        ("request_", "reason"),
        [
            (
                ChatRequest(
                    messages=[user("hi")],
                    tools=[ToolSpec(name="bad", description="x", input_schema={"type": "string"})],
                ),
                "Tool 'bad'",
            ),
            (
                ChatRequest(messages=[user("hi"), results(ADD_RESULT)], tools=[CALCULATOR]),
                "must follow",
            ),
            (
                ChatRequest(
                    messages=[
                        user("hi"),
                        calls(ADD, ToolCallPart(id="call_2", name="calculator")),
                        results(ADD_RESULT),
                    ],
                    tools=[CALCULATOR],
                ),
                "every call",
            ),
            (
                ChatRequest(messages=[user("hi"), calls(ADD), results(ADD_RESULT)]),
                "needs the tools",
            ),
            (
                ChatRequest(
                    messages=[user("hi"), calls(ADD), results(ADD_RESULT), calls(ADD)],
                    tools=[CALCULATOR],
                ),
                "unique",
            ),
            (
                ChatRequest(messages=[user("hi"), calls(ADD), user("and?")], tools=[CALCULATOR]),
                "answered",
            ),
            (ChatRequest(messages=[user("hi")], tool_choice="none"), "needs tools"),
            (
                ChatRequest(
                    messages=[user("hi")],
                    tools=[CALCULATOR],
                    response_format={"type": "json_schema", "schema": {"type": "object"}},
                ),
                "structured output",
            ),
        ],
    )
    async def test_refused_before_any_provider_call(
        self,
        tool_gateway: Any,
        ctx: AIRequestContext,
        provider: MockProvider,
        request_: ChatRequest,
        reason: str,
    ) -> None:
        with pytest.raises(AIInvalidRequest) as raised:
            await tool_gateway.chat(request_, ctx)
        assert reason in raised.value.message
        assert provider.calls == []

    def test_arguments_are_decoded_or_kept_raw(self) -> None:
        assert decode_arguments('{"a": 1}') == ({"a": 1}, None)
        assert decode_arguments("") == ({}, None)
        assert decode_arguments("[1, 2]") == ({}, "[1, 2]")
        assert decode_arguments("not json") == ({}, "not json")
        nested = '{"a":' * 20 + "1" + "}" * 20
        assert decode_arguments(nested)[0] == {}

    def test_definitions_and_calls_count_as_input(self) -> None:
        assert estimate_tools_tokens([CALCULATOR]) > 10
        plain = estimate_message_tokens([calls(ToolCallPart(id="c", name="calculator"))])
        larger = estimate_message_tokens([calls(ADD)])
        assert larger > plain


class TestRouting:
    async def test_a_model_without_tool_calling_is_never_asked(
        self, tool_gateway: Any, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        request = ChatRequest(model="nexa-plain", messages=[user("hi")], tools=[CALCULATOR])
        with pytest.raises(AIModelUnavailable) as raised:
            await tool_gateway.chat(request, ctx)
        assert "tool_calling" in raised.value.message
        assert provider.calls == []

    async def test_capabilities_are_reported_per_model(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        assert Capability.TOOL_CALLING in await tool_gateway.model_capabilities(ctx, None)
        plain = await tool_gateway.model_capabilities(ctx, "nexa-plain")
        assert Capability.TOOL_CALLING not in plain
        assert await tool_gateway.model_capabilities(ctx, "nexa-unknown") == frozenset()


class TestMockLoop:
    async def test_a_call_then_an_answer_from_its_result(
        self, tool_gateway: Any, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        first = await tool_gateway.chat(
            ChatRequest(
                messages=[user('Add these [[tool:calculator {"expression": "2+2"}]]')],
                tools=[CALCULATOR, CLOCK],
            ),
            ctx,
        )
        assert first.finish_reason is FinishReason.TOOL_CALLS
        assert first.content == ""
        [call] = first.tool_calls
        assert (call.id, call.name, call.arguments) == (
            "mock_call_0_0",
            "calculator",
            {"expression": "2+2"},
        )
        second = await tool_gateway.chat(
            ChatRequest(
                messages=[
                    user('Add these [[tool:calculator {"expression": "2+2"}]]'),
                    calls(call),
                    results(ToolResultPart(call_id=call.id, content='{"result_text": "4"}')),
                ],
                tools=[CALCULATOR, CLOCK],
            ),
            ctx,
        )
        assert second.finish_reason is FinishReason.STOP
        assert second.tool_calls == []
        assert second.content == "The result is 4."
        assert [r.status for r in usage.records] == ["success", "success"]

    async def test_streamed_calls_arrive_whole_at_the_end(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        stream = await tool_gateway.open_stream(
            ChatRequest(messages=[user("What is 12 * (3 + 4)?")], tools=[CALCULATOR]), ctx
        )
        events = [event async for event in stream]
        await stream.aclose()
        kinds = [type(e) for e in events]
        assert kinds == [MessageStart, UsageEvent, MessageEnd]
        end = events[-1]
        assert isinstance(end, MessageEnd)
        assert end.finish_reason is FinishReason.TOOL_CALLS
        assert [(c.name, c.arguments) for c in end.tool_calls] == [
            ("calculator", {"expression": "12 * (3 + 4)"})
        ]

    async def test_arguments_that_are_not_an_object_stay_raw(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        response = await tool_gateway.chat(
            ChatRequest(messages=[user("[[tool:calculator two plus two]]")], tools=[CALCULATOR]),
            ctx,
        )
        [call] = response.tool_calls
        assert (call.arguments, call.raw_arguments) == ({}, "two plus two")

    async def test_none_means_an_answer_in_text(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        response = await tool_gateway.chat(
            ChatRequest(messages=[user("What is 2 + 2?")], tools=[CALCULATOR], tool_choice="none"),
            ctx,
        )
        assert response.tool_calls == []
        assert response.content

    async def test_a_misbehaving_model_can_name_a_tool_it_was_not_offered(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        response = await tool_gateway.chat(
            ChatRequest(messages=[user("[[tool:delete_everything {}]]")], tools=[CALCULATOR]),
            ctx,
        )
        assert [c.name for c in response.tool_calls] == ["delete_everything"]

    async def test_without_tools_nothing_changes(
        self, tool_gateway: Any, ctx: AIRequestContext
    ) -> None:
        response = await tool_gateway.chat(ChatRequest(messages=[user("What is 2 + 2?")]), ctx)
        assert response.tool_calls == []
        assert response.finish_reason is FinishReason.STOP


# --- Adapters --------------------------------------------------------------------------------


def tool_call_request() -> ProviderCall:
    request = ChatRequest(
        messages=[
            Message(role=Role.SYSTEM, content="Be brief."),
            user("What is 2+2?"),
            calls(ADD, text="Let me calculate."),
            results(ADD_RESULT),
        ],
        tools=[CALCULATOR],
    )
    return ProviderCall(
        request=request, model="model-x", max_output_tokens=256, timeouts=Timeouts()
    )


class TestAnthropicTools:
    async def test_request_translation(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=claude_message()))
        await anthropic_with(recorder).chat(tool_call_request())
        sent = recorder.body()
        assert sent["tools"] == [
            {
                "name": "calculator",
                "description": CALCULATOR.description,
                "input_schema": CALCULATOR.input_schema,
            }
        ]
        assert sent["tool_choice"] == {"type": "auto"}
        assistant, tool = sent["messages"][1], sent["messages"][2]
        assert assistant == {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me calculate."},
                {"type": "tool_use", "id": "call_1", "name": "calculator", "input": ADD.arguments},
            ],
        }
        assert tool == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": ADD_RESULT.content,
                    "is_error": False,
                }
            ],
        }

    async def test_tool_use_in_a_response(self) -> None:
        recorder = Recorder(
            httpx2.Response(
                200,
                json=claude_message(
                    content=[
                        {"type": "text", "text": "Calculating."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "calculator",
                            "input": {"expression": "6*7"},
                        },
                    ],
                    stop_reason="tool_use",
                ),
            )
        )
        result = await anthropic_with(recorder).chat(tool_call_request())
        assert result.finish_reason is FinishReason.TOOL_CALLS
        assert result.content == "Calculating."
        assert result.tool_calls == (
            ToolCallPart(id="toolu_1", name="calculator", arguments={"expression": "6*7"}),
        )

    async def test_streamed_tool_input_is_joined(self) -> None:
        recorder = Recorder(
            sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            **claude_message(content=[], stop_reason=None),
                            "usage": {"input_tokens": 9, "output_tokens": 1},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_9",
                            "name": "calculator",
                            "input": {},
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": '{"expres'},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": 'sion": "1+1"}'},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 12},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            )
        )
        events = await collect(anthropic_with(recorder).stream(tool_call_request()))
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.finish_reason is FinishReason.TOOL_CALLS
        assert end.tool_calls == (
            ToolCallPart(id="toolu_9", name="calculator", arguments={"expression": "1+1"}),
        )


class TestOpenAITools:
    async def test_request_translation(self) -> None:
        recorder = Recorder(httpx2.Response(200, json=completion()))
        await openai_with(recorder).chat(tool_call_request())
        sent = recorder.body()
        assert sent["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": CALCULATOR.description,
                    "parameters": CALCULATOR.input_schema,
                },
            }
        ]
        assert sent["tool_choice"] == "auto"
        assistant, tool = sent["messages"][2], sent["messages"][3]
        assert assistant["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "calculator", "arguments": json.dumps(ADD.arguments)},
            }
        ]
        assert assistant["content"] == "Let me calculate."
        assert tool == {"role": "tool", "tool_call_id": "call_1", "content": ADD_RESULT.content}

    async def test_tool_calls_in_a_response(self) -> None:
        recorder = Recorder(
            httpx2.Response(
                200,
                json=completion(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"expression":"3"}'},
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": "{broken"},
                        },
                    ],
                ),
            )
        )
        result = await openai_with(recorder).chat(tool_call_request())
        assert result.finish_reason is FinishReason.TOOL_CALLS
        first, second = result.tool_calls
        assert (first.id, first.arguments, first.raw_arguments) == (
            "call_a",
            {"expression": "3"},
            None,
        )
        assert (second.arguments, second.raw_arguments) == ({}, "{broken")

    async def test_streamed_fragments_are_assembled_by_index(self) -> None:
        def fragment(index: int, **parts: Any) -> dict[str, Any]:
            body = chunk()
            body["choices"][0]["delta"] = {"tool_calls": [{"index": index, **parts}]}
            return body

        recorder = Recorder(
            sse(
                (None, fragment(0, id="call_x", type="function", function={"name": "calculator"})),
                (
                    None,
                    fragment(1, id="call_y", type="function", function={"name": "current_time"}),
                ),
                (None, fragment(0, function={"arguments": '{"expression"'})),
                (None, fragment(0, function={"arguments": ': "9/3"}'})),
                (None, fragment(1, function={"arguments": "{}"})),
                (None, chunk(finish="tool_calls")),
                (None, "[DONE]"),
            )
        )
        events = await collect(openai_with(recorder).stream(tool_call_request()))
        end = events[-1]
        assert isinstance(end, StreamEnd)
        assert end.finish_reason is FinishReason.TOOL_CALLS
        assert [(c.id, c.name, c.arguments) for c in end.tool_calls] == [
            ("call_x", "calculator", {"expression": "9/3"}),
            ("call_y", "current_time", {}),
        ]
