"""The `nexa.chat.v1` streaming protocol (docs/api/conversations.md#streaming).

Terminal events, exactly one per stream: `message_complete`, `message_cancelled`, `error`, or
`approval_required` (Phase 10: the reply pauses until the user decides on an action a tool
would take). Every event after `message_start` names the assistant message it is about. A
grounded reply sends `sources` once retrieval is done, before any text (Phase 08). A reply that
uses tools sends `tool_call` when a call is checked and `tool_result` when it ends, only for
calls that really happened (clients ignore event types they do not know, so the additions keep
the protocol version).
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from nexa_runtime.chat.schemas import MessageOut
from nexa_runtime.rag.schemas import SourceOut
from nexa_runtime.tools.schemas import ApprovalOut, ToolStatus

PROTOCOL = "nexa.chat.v1"
PROTOCOL_HEADER = "X-NEXA-Stream-Protocol"


class MessageStart(BaseModel):
    type: Literal["message_start"] = "message_start"
    protocol: str = PROTOCOL
    conversation_id: UUID
    request_id: str | None
    user_message: MessageOut
    assistant_message: MessageOut


class SourcesFound(BaseModel):
    """The passages found for a grounded reply (empty when the documents had nothing)."""

    type: Literal["sources"] = "sources"
    message_id: UUID
    searched_documents: int
    sources: list[SourceOut]


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    message_id: UUID
    text: str


class UsageReport(BaseModel):
    type: Literal["usage"] = "usage"
    message_id: UUID
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    cost_usd: str | None


class ToolCallEvent(BaseModel):
    """A tool call the model asked for, after the checks: about to run (`executing`), refused
    (`rejected`, with the reason), or waiting for the user (`awaiting_approval`)."""

    type: Literal["tool_call"] = "tool_call"
    message_id: UUID
    invocation_id: UUID
    call_id: str
    tool_id: str
    tool_name: str
    step: int
    status: ToolStatus
    summary: str
    risk: str
    error_code: str | None = None
    error_message: str | None = None


class ToolResultEvent(BaseModel):
    """How a tool call ended. `result` is the validated data the model received."""

    type: Literal["tool_result"] = "tool_result"
    message_id: UUID
    invocation_id: UUID
    call_id: str
    tool_id: str
    status: ToolStatus
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    result_truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalRequired(BaseModel):
    """Terminal: the reply is paused (`awaiting_approval`) until the user approves or rejects
    the action (POST /conversations/{id}/approvals/{approval_id}/), which continues it."""

    type: Literal["approval_required"] = "approval_required"
    message: MessageOut
    approval: ApprovalOut


class MessageComplete(BaseModel):
    type: Literal["message_complete"] = "message_complete"
    message: MessageOut


class MessageCancelled(BaseModel):
    type: Literal["message_cancelled"] = "message_cancelled"
    message: MessageOut


class GenerationError(BaseModel):
    type: Literal["error"] = "error"
    message_id: UUID
    code: str
    message: str
    retryable: bool
    retry_after: int | None = None
    request_id: str | None
    assistant_message: MessageOut


ChatEvent = (
    MessageStart
    | SourcesFound
    | TextDelta
    | UsageReport
    | ToolCallEvent
    | ToolResultEvent
    | ApprovalRequired
    | MessageComplete
    | MessageCancelled
    | GenerationError
)
