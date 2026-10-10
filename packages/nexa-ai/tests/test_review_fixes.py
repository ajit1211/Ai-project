"""Regression tests for defects found in the Phase 04 code review."""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIProviderError, AIUnsupportedByProvider
from nexa_ai.gateway import Gateway, MemoryUsageSink, UsageRecord
from nexa_ai.providers.anthropic import AnthropicProvider
from nexa_ai.providers.base import ProviderCall, ProviderEvent, StreamStart, Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.registry import OrganizationPolicy
from nexa_ai.sse import sse_bytes
from nexa_ai.structured import OutputMismatch, check_schema, parse_structured
from nexa_ai.testing import StaticCatalog, ask, make_registry
from nexa_ai.types import ChatRequest, Message, MessageDelta, Role

pytestmark = pytest.mark.anyio

GatewayFactory = Callable[..., Gateway]


async def test_anthropic_schema_it_cannot_express_falls_back_instead_of_failing() -> None:
    """A valid JSON Schema the SDK's transformer rejects is the provider's limitation."""

    def unreachable(_: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request should be sent")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(unreachable))
    provider = AnthropicProvider(api_key="k", http_client=client)
    schema = {"type": "object", "properties": {"note": {"description": "free text"}}}
    check_schema(schema)  # acceptable to NEXA
    call = ProviderCall(
        request=ChatRequest(
            messages=[Message(role=Role.USER, content="hi")],
            response_format={"type": "json_schema", "schema": schema},
        ),
        model="claude-x",
        max_output_tokens=100,
        timeouts=Timeouts(),
    )
    with pytest.raises(AIUnsupportedByProvider) as caught:
        await provider.chat(call)
    assert caught.value.fallback_eligible


class TestStructuredSafety:
    def test_mismatch_summary_never_quotes_model_output(self) -> None:
        schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
        with pytest.raises(OutputMismatch) as caught:
            parse_structured('{"age": "SECRET-PHONE-555-0100"}', schema)
        assert "SECRET" in caught.value.reason  # sent back to the model only
        assert "SECRET" not in caught.value.summary  # what logs and errors carry
        assert "type" in caught.value.summary

    @pytest.mark.parametrize("name", ["pattern", "$ref", "$id", "enum", "properties"])
    def test_property_names_are_not_keywords(self, name: str) -> None:
        check_schema({"type": "object", "properties": {name: {"type": "string"}}})

    def test_data_is_not_inspected_for_keywords(self) -> None:
        check_schema(
            {
                "type": "object",
                "properties": {"kind": {"enum": [{"pattern": "x"}, "$ref"]}},
                "default": {"$id": "http://example.com"},
            }
        )

    def test_keywords_inside_named_subschemas_are_still_checked(self) -> None:
        from nexa_ai.errors import AIInvalidRequest

        with pytest.raises(AIInvalidRequest, match="pattern"):
            check_schema(
                {"type": "object", "properties": {"a": {"type": "string", "pattern": "(a+)+$"}}}
            )
        with pytest.raises(AIInvalidRequest, match="local"):
            check_schema(
                {"type": "object", "$defs": {"x": {"$ref": "http://evil/"}}, "properties": {}}
            )


async def test_closing_the_sse_stream_closes_the_gateway_stream() -> None:
    closed = asyncio.Event()

    async def events() -> AsyncIterator[MessageDelta]:
        try:
            while True:
                yield MessageDelta(text="x")
        finally:
            closed.set()

    stream = sse_bytes(events())
    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
    assert closed.is_set()  # at once, not when garbage-collected


async def test_stream_accepted_then_broken_is_metered(
    make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
) -> None:
    class AcceptsThenFails(MockProvider):
        async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
            if call.model == "mock-primary":
                yield StreamStart(input_tokens=500)
                raise AIProviderError(provider="mock")
            async for event in super().stream(call):
                yield event

    gateway = make_gateway(providers={"mock": AcceptsThenFails()})
    events = [e async for e in await gateway.open_stream(ask("hi"), ctx)]
    assert events[-1].type == "message.end"  # type: ignore[union-attr]
    record = usage.records[-1]
    # Three primary attempts (one try, two retries) each accepted 500 input tokens.
    assert record.input_tokens >= 3 * 500
    assert record.usage_source == "estimated"


async def test_final_ledger_write_survives_cancellation(ctx: AIRequestContext) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    class SlowSink:
        def __init__(self) -> None:
            self.records: list[UsageRecord] = []

        async def record(self, usage: UsageRecord) -> None:
            started.set()
            await release.wait()
            self.records.append(usage)

    sink = SlowSink()
    gateway = Gateway(
        providers={"mock": MockProvider()},
        catalog=StaticCatalog(make_registry()),
        usage=sink,
    )
    task = asyncio.create_task(gateway.chat(ask("hi"), ctx))
    await started.wait()  # the answer is done; the ledger write is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await gateway.drain()
    assert [r.status for r in sink.records] == ["success"]


async def test_zero_output_cap_is_respected(
    make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
) -> None:
    catalog = StaticCatalog(
        make_registry(), {ctx.organization_id: OrganizationPolicy(max_output_tokens=0)}
    )
    await make_gateway(catalog=catalog).chat(ask("hi", max_output_tokens=500), ctx)
    assert provider.calls[-1].max_output_tokens == 1  # the smallest possible, not the default
