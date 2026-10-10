"""The `nexa.chat.v1` streaming protocol (docs/api/conversations.md#streaming).

Terminal events, exactly one per stream: `message_complete`, `message_cancelled` or `error`.
Every event after `message_start` names the assistant message it is about.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from nexa_runtime.chat.schemas import MessageOut

PROTOCOL = "nexa.chat.v1"
PROTOCOL_HEADER = "X-NEXA-Stream-Protocol"


class MessageStart(BaseModel):
    type: Literal["message_start"] = "message_start"
    protocol: str = PROTOCOL
    conversation_id: UUID
    request_id: str | None
    user_message: MessageOut
    assistant_message: MessageOut


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
    MessageStart | TextDelta | UsageReport | MessageComplete | MessageCancelled | GenerationError
)
