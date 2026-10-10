"""Streaming through the gateway: event order, fallback before the first token, errors after
it, timeouts, keep-alives, cancellation and SSE encoding."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import pytest

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIInvalidRequest, AIModelUnavailable
from nexa_ai.gateway import Gateway, KeepAlive, MemoryUsageSink
from nexa_ai.providers.base import ProviderCall, ProviderEvent, StreamEnd, TextChunk, Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.sse import PING, encode_event, sse_bytes
from nexa_ai.testing import ask
from nexa_ai.types import (
    FinishReason,
    MessageDelta,
    MessageEnd,
    MessageStart,
    StreamError,
    StreamEvent,
    Usage,
    UsageEvent,
)

pytestmark = pytest.mark.anyio

GatewayFactory = Callable[..., Gateway]


async def collect(events: AsyncIterator[StreamEvent | KeepAlive]) -> list[StreamEvent | KeepAlive]:
    return [event async for event in events]


def kinds(events: list[StreamEvent | KeepAlive]) -> list[str]:
    return ["ping" if isinstance(e, KeepAlive) else e.type for e in events]


class TestStreaming:
    async def test_event_order_and_content(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        gateway = make_gateway()
        events = await collect(await gateway.open_stream(ask("Explain SSE"), ctx))

        names = kinds(events)
        assert names[0] == "message.start"
        assert names[-2:] == ["usage", "message.end"]
        assert set(names[1:-2]) == {"message.delta"}
        assert len(names[1:-2]) > 3  # really chunked

        start = events[0]
        assert isinstance(start, MessageStart)
        assert start.model == "nexa-balanced"
        assert start.provider_model == "mock-primary"
        assert start.request_id == "req_test"

        text = "".join(e.text for e in events if isinstance(e, MessageDelta))
        complete = await gateway.chat(ask("Explain SSE"), ctx)
        assert text == complete.content  # streaming and non-streaming agree

        end = events[-1]
        assert isinstance(end, MessageEnd)
        assert end.finish_reason is FinishReason.STOP
        stream_usage = events[-2]
        assert isinstance(stream_usage, UsageEvent)
        assert stream_usage.usage.total_tokens > 0

        record = usage.records[0]
        assert record.stream is True
        assert record.status == "success"
        assert record.ttft_ms is not None
        assert record.usage_source == "provider"

    async def test_failure_before_the_first_token_falls_back(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        provider = MockProvider(failures={"mock-primary": ["provider", "provider", "provider"]})
        events = await collect(
            await make_gateway(providers={"mock": provider}).open_stream(ask("hi"), ctx)
        )
        start = events[0]
        assert isinstance(start, MessageStart)
        assert start.provider_model == "mock-backup"
        end = events[-1]
        assert isinstance(end, MessageEnd)
        assert end.fallback_used is True
        assert end.attempts == 4
        assert usage.records[-1].fallback_used is True

    async def test_failure_after_the_first_token_is_an_error_event(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        events = await collect(
            await make_gateway().open_stream(ask("[[fail:stream_after:2]] hi"), ctx)
        )
        names = kinds(events)
        assert names == ["message.start", "message.delta", "message.delta", "usage", "error"]
        error = events[-1]
        assert isinstance(error, StreamError)
        assert error.code == "AI_PROVIDER_UNAVAILABLE"
        # No fallback: the client already has text from the first model.
        assert all(
            not isinstance(e, MessageStart) or e.provider_model == "mock-primary" for e in events
        )
        record = usage.records[-1]
        assert record.status == "error"
        assert record.usage_source == "estimated"
        assert record.output_tokens > 0

    async def test_non_fallback_errors_end_the_stream(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        events = await collect(await make_gateway().open_stream(ask("[[fail:content]]"), ctx))
        assert kinds(events) == ["usage", "error"]
        error = events[-1]
        assert isinstance(error, StreamError)
        assert error.code == "AI_CONTENT_REJECTED"
        assert error.retryable is False

    async def test_every_candidate_failing(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        events = await collect(await make_gateway().open_stream(ask("[[fail:timeout]]"), ctx))
        assert kinds(events) == ["usage", "error"]
        assert isinstance(events[-1], StreamError)
        assert events[-1].code == "AI_TIMEOUT"

    async def test_errors_before_any_provider_call_are_raised(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        gateway = make_gateway()
        with pytest.raises(AIModelUnavailable):
            await gateway.open_stream(ask("hi", model="nexa-basic"), ctx)  # no streaming
        with pytest.raises(AIInvalidRequest):
            await gateway.open_stream(
                ask("hi", response_format={"type": "json_schema", "schema": {"type": "object"}}),
                ctx,
            )

    async def test_first_token_timeout_falls_back_and_pings(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        class Stalling(MockProvider):
            """The primary model never sends anything; the backup answers."""

            async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
                if call.model == "mock-primary":
                    await asyncio.sleep(10)
                async for event in super().stream(call):
                    yield event

        gateway = make_gateway(
            providers={"mock": Stalling()},
            timeouts=Timeouts(first_token=0.2),
            ping_interval=0.05,
        )
        events = await collect(await gateway.open_stream(ask("hi"), ctx))
        names = kinds(events)
        assert "ping" in names[: names.index("message.start")]
        start = events[names.index("message.start")]
        assert isinstance(start, MessageStart)
        assert start.provider_model == "mock-backup"

    async def test_idle_timeout_after_the_first_token(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        class Freezing(MockProvider):
            async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
                yield TextChunk("Hello ")
                await asyncio.sleep(10)
                yield StreamEnd(finish_reason=FinishReason.STOP)

        gateway = make_gateway(providers={"mock": Freezing()}, timeouts=Timeouts(idle=0.1))
        events = await collect(await gateway.open_stream(ask("hi"), ctx))
        assert kinds(events) == ["message.start", "message.delta", "usage", "error"]
        assert isinstance(events[-1], StreamError)
        assert events[-1].code == "AI_TIMEOUT"

    async def test_missing_usage_is_estimated(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        class NoUsage(MockProvider):
            async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
                yield TextChunk("twelve chars")
                yield StreamEnd(finish_reason=FinishReason.STOP, usage=None)

        events = await collect(
            await make_gateway(providers={"mock": NoUsage()}).open_stream(ask("hi"), ctx)
        )
        assert kinds(events)[-1] == "message.end"
        record = usage.records[-1]
        assert record.usage_source == "estimated"
        assert record.output_tokens == 4  # twelve characters at three per token
        assert record.input_tokens > 0

    async def test_client_disconnect_is_recorded_as_cancelled(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        gateway = make_gateway(providers={"mock": MockProvider()})
        stream = await gateway.open_stream(ask("[[slow:50]] a long answer please"), ctx)
        seen = []
        async for event in stream:
            seen.append(event)
            if isinstance(event, MessageDelta):
                break  # the client goes away after the first delta
        await stream.aclose()
        await gateway.drain()
        record = usage.records[-1]
        assert record.status == "cancelled"
        assert record.output_tokens > 0
        assert record.usage_source == "estimated"

    async def test_cancelled_task_is_recorded(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        gateway = make_gateway(providers={"mock": MockProvider()})

        async def consume() -> None:
            async for _ in await gateway.open_stream(ask("[[slow:2000]] hi"), ctx):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await gateway.drain()
        assert usage.records[-1].status == "cancelled"

    async def test_an_error_is_preceded_by_what_the_call_used(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        stream = await make_gateway().open_stream(ask("[[fail:stream_after:2]] hi"), ctx)
        events = await collect(stream)
        reported = events[-2]
        assert isinstance(reported, UsageEvent)
        record = usage.records[-1]
        assert record.status == "error"
        assert reported.usage.output_tokens == record.output_tokens > 0
        # The stream object carries the same numbers and the ledger row's id.
        assert stream.usage == reported.usage
        assert stream.call_id == record.id

    async def test_the_call_id_is_known_before_the_first_event(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        stream = await make_gateway().open_stream(ask("hi"), ctx)
        call_id = stream.call_id
        before = stream.usage  # nothing is metered yet
        assert before is None
        events = await collect(stream)
        start = events[0]
        assert isinstance(start, MessageStart)
        assert start.id == str(call_id)
        assert usage.records[-1].id == call_id
        assert stream.usage is not None
        assert stream.usage.total_tokens == usage.records[-1].total_tokens

    async def test_closing_an_unread_stream_releases_its_reservation(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        settled: list[tuple[object, int]] = []

        class Recording:
            async def reserve(self, *_: object, **__: object) -> object:
                return "ticket-1"

            async def settle(self, ticket: object, *, tokens_used: int) -> None:
                settled.append((ticket, tokens_used))

        gateway = make_gateway(quota=Recording())
        stream = await gateway.open_stream(ask("hi"), ctx)
        await stream.aclose()  # before reading a single event
        await stream.aclose()  # closing twice is harmless
        await gateway.drain()
        assert settled == [("ticket-1", 0)]
        record = usage.records[-1]
        assert record.id == stream.call_id
        assert record.status == "cancelled"
        assert record.total_tokens == 0
        assert record.attempts == 0
        assert stream.usage is not None
        assert stream.usage.total_tokens == 0


class TestSSE:
    def test_event_encoding(self) -> None:
        body = encode_event(MessageDelta(text="Hi é"), 7).decode()
        assert body == 'event: message.delta\nid: 7\ndata: {"text":"Hi é"}\n\n'

    def test_usage_is_flattened(self) -> None:
        body = encode_event(
            UsageEvent(usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3)), 1
        )
        data = json.loads(body.decode().split("data: ", 1)[1])
        assert data["total_tokens"] == 3
        assert "usage" not in data

    async def test_stream_numbers_events_and_sends_pings(self) -> None:
        async def events() -> AsyncIterator[StreamEvent | KeepAlive]:
            yield MessageStart(id="a", model="m", provider="p", provider_model="pm")
            yield KeepAlive()
            yield MessageDelta(text="x")

        chunks = [chunk async for chunk in sse_bytes(events())]
        assert chunks[1] == PING
        assert b"id: 1\n" in chunks[0]
        assert b"id: 2\n" in chunks[2]

    def test_no_provider_details_in_error_events(self) -> None:
        body = encode_event(StreamError(code="AI_TIMEOUT", message="slow"), 1).decode()
        payload = json.loads(body.split("data: ", 1)[1])
        assert set(payload) == {"code", "message", "retryable", "request_id"}
