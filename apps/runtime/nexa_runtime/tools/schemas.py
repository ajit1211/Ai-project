"""Shapes of the tool API and of tool activity on messages (docs/api/tools.md)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ToolStatus = Literal[
    "requested",
    "validated",
    "awaiting_approval",
    "approved",
    "executing",
    "succeeded",
    "rejected",
    "failed",
    "timed_out",
    "cancelled",
]


class ApprovalOut(BaseModel):
    """An action waiting for (or past) the user's decision. `arguments_hash` identifies the
    exact action: a decision must send it back, so a changed action is never approved by an
    old decision."""

    id: UUID
    invocation_id: UUID
    tool_id: str
    tool_name: str
    summary: str
    details: dict[str, Any]
    risk: str
    status: Literal["pending", "approved", "rejected", "expired", "invalidated"]
    arguments_hash: str
    expires_at: datetime
    decided_at: datetime | None = None


class ToolActivityOut(BaseModel):
    """One tool call of a reply, as its owner may see it."""

    invocation_id: UUID
    call_id: str
    tool_id: str
    tool_name: str
    tool_version: str
    step: int
    position: int
    status: ToolStatus
    summary: str
    risk: str
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    # The validated result, as the model received it; null when it failed, or when a document
    # it came from is no longer readable by the viewer (`result_hidden`).
    result: dict[str, Any] | None = None
    result_truncated: bool = False
    result_hidden: bool = False
    sources: list[dict[str, Any]] = Field(default_factory=list)
    approval: ApprovalOut | None = None


class ToolOut(BaseModel):
    id: str
    name: str
    description: str
    version: str
    category: str
    risk: str
    read_only: bool
    requires_approval: bool
    enabled: bool
    # Enabled, permitted for the caller, and the caller may let AI use tools at all.
    available: bool
    disabled_by: Literal["platform", "organization", None] = None
    permissions: list[str]


class ToolDetailOut(ToolOut):
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout_seconds: float
    max_retries: int


class ToolSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    # The `arguments_hash` of the approval the user was shown.
    arguments_hash: str = Field(min_length=64, max_length=64)


class InvocationOut(ToolActivityOut):
    # The owner: a chat reply, or an agent run (Phase 11).
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    run_id: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None
