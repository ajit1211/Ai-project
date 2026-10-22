"""Request and response shapes for the conversation API (docs/api/conversations.md)."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from nexa_ai.cost import micros_to_usd
from nexa_runtime.rag.schemas import GroundingOut, GroundingRequest, SourceOut
from nexa_runtime.tools.schemas import ToolActivityOut

MAX_CONTENT_CHARS = 100_000
_MODEL_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"
_CLIENT_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"


def _reject_nul(value: str) -> str:
    # PostgreSQL text cannot hold NUL: refuse it here (422) rather than fail in the database.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


NulFree = Annotated[str, AfterValidator(_reject_nul)]

ConversationStatus = Literal["active", "archived"]
MessageStatus = Literal[
    "pending", "streaming", "completed", "failed", "cancelled", "awaiting_approval"
]
ToolUse = Literal["auto", "none"]


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NulFree = Field(default="", max_length=200)
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class UpdateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NulFree | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, pattern=_MODEL_PATTERN)
    status: ConversationStatus | None = None


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: NulFree = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    client_message_id: str = Field(default="", pattern=_CLIENT_ID_PATTERN)
    model: str = Field(default="", pattern=_MODEL_PATTERN)
    # Answer from the user's documents (Phase 08); absent for an ordinary reply.
    grounding: GroundingRequest | None = None
    # Phase 10: "auto" lets the model use the tools this user may use (streamed replies of
    # ordinary chat); "none" answers without tools. Grounded replies never use tools.
    tools: ToolUse = "auto"


class Regenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="", pattern=_MODEL_PATTERN)


class ConversationOut(BaseModel):
    id: UUID
    title: str
    model: str
    status: ConversationStatus
    message_count: int
    generating: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> "ConversationOut":
        return cls(
            id=row.id,
            title=row.title,
            model=row.model,
            status=row.status,
            message_count=row.message_count,
            generating=row.active_generation_id is not None,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_message_at=row.last_message_at,
        )


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sequence: int
    role: Literal["system", "user", "assistant"]
    content: str
    status: MessageStatus
    client_message_id: str
    model: str
    provider: str
    provider_model: str
    input_tokens: int
    output_tokens: int
    cost_usd: str | None
    latency_ms: int
    ttft_ms: int | None
    finish_reason: str
    error_code: str | None
    request_id: str
    created_at: datetime
    completed_at: datetime | None
    # Document-grounded replies only: the scope and outcome, and the passages given to the
    # model (re-authorized whenever messages are read).
    grounding: GroundingOut | None = None
    sources: list[SourceOut] = Field(default_factory=list)
    # Replies that used tools (Phase 10): every call, in order, as its owner may see it.
    tools: list[ToolActivityOut] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> "MessageOut":
        return cls(
            id=row.id,
            conversation_id=row.conversation_id,
            sequence=row.sequence,
            role=row.role,
            content=row.content,
            status=row.status,
            client_message_id=row.client_message_id,
            model=row.model,
            provider=row.provider,
            provider_model=row.provider_model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=micros_to_usd(row.cost_micros) if row.cost_micros is not None else None,
            latency_ms=row.latency_ms,
            ttft_ms=row.ttft_ms,
            finish_reason=row.finish_reason,
            error_code=row.error_code or None,
            request_id=row.request_id,
            created_at=row.created_at,
            completed_at=row.completed_at,
            grounding=GroundingOut.from_record(getattr(row, "grounding", None)),
        )


class Exchange(BaseModel):
    """The non-streaming answer to sending a message."""

    user_message: MessageOut
    assistant_message: MessageOut


class CancelResult(BaseModel):
    cancelled: bool
    message_id: UUID | None
