"""Canonical AI types: what callers send, what they get back, and what a stream carries.

These are NEXA's own shapes (architecture doc 12.1). Adapters translate them to and from each
provider's API, so switching providers never touches calling code. Roles and parts for later
phases (images, tool calls) are declared now so the schema does not change when they arrive;
the gateway refuses what it cannot serve yet with `AI_INVALID_REQUEST`.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MESSAGES = 200
MAX_TEXT_CHARS = 400_000  # ~100k tokens; models with smaller windows reject earlier
MAX_STOP_SEQUENCES = 4
DEFAULT_MAX_OUTPUT_TOKENS = 1024

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Capability(StrEnum):
    """What a model can do. Phase 04 exercises chat, streaming and structured output."""

    CHAT = "chat"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    REASONING = "reasoning"
    AUDIO = "audio"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Feature(StrEnum):
    """Which NEXA feature made the call. A closed set, so usage analytics stay meaningful and
    a client cannot pick a label with looser limits. `chat` is set by the chat engine, never
    by a client; later phases add `title` and `agent` the same way."""

    API = "api"
    PLAYGROUND = "playground"
    CHAT = "chat"
    # Set by the memory engine (Phase 06): extraction after a turn, and conversation summaries.
    MEMORY = "memory"
    SUMMARY = "summary"
    # Set by the chat engine for a reply grounded in the user's documents (Phase 08).
    RAG = "rag"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    ERROR = "error"


# --- Message parts -------------------------------------------------------------------------


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(max_length=MAX_TEXT_CHARS)


class ImagePart(BaseModel):
    """An image by URL or inline base64. Declared for Phase 05; refused until then."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    url: str | None = Field(default=None, max_length=2048)
    data: str | None = Field(default=None, max_length=20_000_000)
    media_type: str = Field(default="image/png", max_length=64)


class ToolCallPart(BaseModel):
    """A tool the model wants to call. Declared for Phase 10."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    id: str = Field(max_length=128)
    name: str = Field(max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultPart(BaseModel):
    """The result of a tool call, sent back to the model. Declared for Phase 10."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    call_id: str = Field(max_length=128)
    content: str = Field(max_length=MAX_TEXT_CHARS)
    is_error: bool = False


Part = Annotated[TextPart | ImagePart | ToolCallPart | ToolResultPart, Field(discriminator="type")]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | list[Part] = Field(max_length=MAX_TEXT_CHARS)

    @property
    def parts(self) -> list[Part]:
        if isinstance(self.content, str):
            return [TextPart(text=self.content)]
        return list(self.content)

    @property
    def text(self) -> str:
        """The text parts joined; used for token estimates and text-only providers."""
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))


# --- Response format -----------------------------------------------------------------------


class TextFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"


class JSONSchemaFormat(BaseModel):
    """Ask for a JSON document matching `schema`. Validated by the gateway on the way back."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["json_schema"] = "json_schema"
    name: str = Field(default="response", pattern=_NAME.pattern)
    json_schema: dict[str, Any] = Field(alias="schema")


ResponseFormat = Annotated[TextFormat | JSONSchemaFormat, Field(discriminator="type")]


# --- Request -------------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """A normalized chat request. `model` is a logical name such as `nexa-fast`."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, pattern=_NAME.pattern)
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    response_format: ResponseFormat | None = None
    stop: list[str] | None = Field(default=None, max_length=MAX_STOP_SEQUENCES)
    # Metadata for the usage ledger only; it never affects authorization.
    feature: Feature = Feature.API

    @model_validator(mode="after")
    def _at_least_one_turn(self) -> "ChatRequest":
        if all(message.role is Role.SYSTEM for message in self.messages):
            raise ValueError("messages must include at least one user or assistant message")
        if self.stop is not None and any(not s or len(s) > 64 for s in self.stop):
            raise ValueError("stop sequences must be 1 to 64 characters")
        return self

    @property
    def wants_structured_output(self) -> bool:
        return isinstance(self.response_format, JSONSchemaFormat)


# --- Response ------------------------------------------------------------------------------


class Usage(BaseModel):
    """`input_tokens` includes cached and cache-write input; `total_tokens` = input + output."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    # Decimal string in USD with six places, e.g. "0.001234"; null when no price is known.
    cost_usd: str | None = None


class ChatResponse(BaseModel):
    id: str
    model: str
    provider: str
    provider_model: str
    content: str
    parsed: Any | None = None
    finish_reason: FinishReason
    usage: Usage
    latency_ms: int
    attempts: int = 1
    fallback_used: bool = False


# --- Stream events (what the SSE endpoint sends) --------------------------------------------


class MessageStart(BaseModel):
    type: Literal["message.start"] = "message.start"
    id: str
    model: str
    provider: str
    provider_model: str
    request_id: str | None = None


class MessageDelta(BaseModel):
    type: Literal["message.delta"] = "message.delta"
    text: str


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: Usage


class MessageEnd(BaseModel):
    type: Literal["message.end"] = "message.end"
    finish_reason: FinishReason
    latency_ms: int
    attempts: int = 1
    fallback_used: bool = False


class StreamError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool = False
    request_id: str | None = None


StreamEvent = MessageStart | MessageDelta | UsageEvent | MessageEnd | StreamError
