"""The contract every provider adapter implements.

An adapter does three things and nothing else: translate a `ProviderCall` into the
provider's request, translate the provider's answer or stream into NEXA's shapes, and map the
provider's failures to `nexa_ai.errors`. Retries, fallback, timeouts beyond the SDK's own,
quotas, metering and logging all belong to the gateway, so they behave identically for every
provider.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from nexa_ai.types import ChatRequest, FinishReason, JSONSchemaFormat, Message, ToolCallPart


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Seconds.

    connect      opening a connection to the provider
    request      one non-streaming attempt, end to end
    first_token  streaming: from the request until the provider shows any activity
    idle         streaming: the longest gap between two provider events
    total        a whole gateway call, every attempt, retry and fallback included
    """

    connect: float = 5.0
    request: float = 120.0
    first_token: float = 45.0
    idle: float = 30.0
    total: float = 300.0


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """One attempt at one concrete model, with everything the gateway has already decided."""

    request: ChatRequest
    model: str
    max_output_tokens: int
    timeouts: Timeouts
    # None when the caller did not set one or the model does not accept it.
    temperature: float | None = None
    # An opaque, stable identifier for abuse reporting at the provider; never an email or a
    # raw user ID.
    end_user_id: str | None = None
    # The gateway's request ID, forwarded to the provider where supported.
    request_id: str | None = None

    @property
    def response_format(self) -> JSONSchemaFormat | None:
        fmt = self.request.response_format
        return fmt if isinstance(fmt, JSONSchemaFormat) else None

    @property
    def messages(self) -> list[Message]:
        return list(self.request.messages)


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """`input_tokens` is all input: uncached, read from cache and written to cache.

    Providers report these differently (OpenAI's prompt tokens include cached tokens;
    Anthropic reports the three separately); adapters normalize to this shape.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ProviderResult:
    content: str
    finish_reason: FinishReason
    usage: TokenCounts
    provider_response_id: str | None = None
    # The tool calls of the answer, in the order the model made them (Phase 10).
    tool_calls: tuple[ToolCallPart, ...] = ()


# Stream items an adapter yields. The gateway turns them into API events.


@dataclass(frozen=True, slots=True)
class StreamStart:
    provider_response_id: str | None = None
    # Some providers report input tokens at the start of the stream.
    input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str


@dataclass(frozen=True, slots=True)
class Activity:
    """The provider is alive but produced no answer text (a ping, a thinking block).

    Resets the first-token and idle clocks; never reaches the client."""


@dataclass(frozen=True, slots=True)
class StreamEnd:
    finish_reason: FinishReason
    # None when the provider did not report usage; the gateway estimates instead.
    usage: TokenCounts | None = None
    # Tool calls are assembled by the adapter from the provider's fragments and reported
    # whole, once, at the end of the stream.
    tool_calls: tuple[ToolCallPart, ...] = ()


ProviderEvent = StreamStart | TextChunk | Activity | StreamEnd

ACTIVITY = Activity()


class AIProvider(Protocol):
    """One adapter per provider; one instance per process."""

    name: str

    async def chat(self, call: ProviderCall) -> ProviderResult: ...

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]: ...

    async def aclose(self) -> None: ...
