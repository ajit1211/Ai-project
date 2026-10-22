"""Request and response shapes of the runs API (docs/api/agents.md#runs)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexa_ai.cost import micros_to_usd
from nexa_runtime.chat.schemas import NulFree
from nexa_runtime.tools.schemas import ApprovalOut, ToolActivityOut

MAX_GOAL_CHARS = 10_000
MAX_INPUT_CHARS = 50_000

RunStatus = Literal[
    "queued",
    "running",
    "waiting_for_approval",
    "paused",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]
StepKind = Literal["decision", "tool", "approval", "progress", "final", "error"]
StepStatus = Literal["running", "succeeded", "failed", "rejected", "cancelled", "waiting"]

_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    goal: NulFree = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    # Validated against the agent's input schema, when it has one.
    input: dict[str, Any] = Field(default_factory=dict)
    conversation_id: UUID | None = None
    # A retried submission with the same key returns the earlier run instead of a new one.
    idempotency_key: str = Field(default="", pattern=_IDEMPOTENCY_PATTERN)

    @field_validator("input")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > MAX_INPUT_CHARS:
            raise ValueError("input is too large")
        return value


class RunLimits(BaseModel):
    max_steps: int
    max_tool_calls: int
    max_duration_seconds: int
    max_tokens: int


class RunUsage(BaseModel):
    """Measured from the usage ledger and the step records; `cost_usd` is null when a model
    has no price. Nothing here is estimated."""

    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: str | None


class RunSource(BaseModel):
    """A passage the answer rests on: references, re-authorized for the reader now."""

    ref: str
    document_id: UUID
    chunk_id: UUID | None = None
    title: str = ""
    available: bool = True


class RunOut(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    agent_version: int
    status: RunStatus
    goal: str
    input: dict[str, Any]
    conversation_id: UUID | None
    model: str
    progress_message: str
    step_count: int
    tool_call_count: int
    limits: RunLimits
    usage: RunUsage
    result: str
    output: dict[str, Any] | None
    sources: list[RunSource]
    failure_reason: str | None
    error_message: str | None
    cancel_requested: bool
    pause_requested: bool
    pending_approval: ApprovalOut | None = None
    # What the caller may do now, so clients show the right controls.
    can_pause: bool
    can_resume: bool
    can_cancel: bool
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    # While running: when its time budget ends. While paused or waiting: None, and
    # time_left_seconds says what it has left (the clock is stopped).
    deadline_at: datetime | None
    time_left_seconds: int | None = None

    @staticmethod
    def usage_of(row: Any) -> RunUsage:
        return RunUsage(
            model_calls=row.model_calls,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=micros_to_usd(row.cost_micros) if row.cost_micros is not None else None,
        )


class StepOut(BaseModel):
    id: UUID
    sequence: int
    kind: StepKind
    status: StepStatus
    summary: str
    detail: dict[str, Any]
    tool: ToolActivityOut | None = None
    error_code: str | None
    retry_count: int
    input_tokens: int
    output_tokens: int
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class RunDecision(BaseModel):
    """A decision on a run's pending action (the same contract as chat approvals)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    arguments_hash: str = Field(min_length=64, max_length=64)
    comment: NulFree = Field(default="", max_length=500)


class RunControlOut(BaseModel):
    """The run after a pause, resume or cancel request."""

    id: UUID
    status: RunStatus
    cancel_requested: bool
    pause_requested: bool
    message: str
