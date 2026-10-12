"""Request and response shapes of the memory API (docs/api/memories.md)."""

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from nexa_runtime.chat.schemas import NulFree

MAX_CONTENT_CHARS = 2_000
MAX_KEY_CHARS = 80
MAX_VALUE_BYTES = 4_096
_KEY_PATTERN = r"^[A-Za-z0-9 _.:/-]{0,80}$"

MemoryScope = Literal["conversation", "user", "team", "organization"]
MemoryKind = Literal["preference", "fact", "decision", "episode"]
MemorySourceType = Literal["explicit", "inferred", "assistant", "manual", "summary"]
MemoryStatus = Literal["candidate", "active", "superseded", "expired", "rejected"]

PRIVATE_SCOPES: frozenset[str] = frozenset({"conversation", "user"})


def _small_json(value: JsonValue) -> JsonValue:
    if value is not None and len(json.dumps(value)) > MAX_VALUE_BYTES:
        raise ValueError(f"must serialize to at most {MAX_VALUE_BYTES} bytes")
    return value


class CreateMemory(BaseModel):
    """A memory the user writes down. It is active at once: the user is the source."""

    model_config = ConfigDict(extra="forbid")

    scope: MemoryScope = "user"
    kind: MemoryKind = "fact"
    key: NulFree = Field(default="", max_length=MAX_KEY_CHARS, pattern=_KEY_PATTERN)
    content: NulFree = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    value: JsonValue = None
    importance: int = Field(default=3, ge=1, le=5)
    team_id: UUID | None = None
    conversation_id: UUID | None = None
    expires_at: datetime | None = None

    @field_validator("value")
    @classmethod
    def _value_size(cls, value: JsonValue) -> JsonValue:
        return _small_json(value)


class UpdateMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MemoryKind | None = None
    key: NulFree | None = Field(default=None, max_length=MAX_KEY_CHARS, pattern=_KEY_PATTERN)
    content: NulFree | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_CHARS)
    value: JsonValue = None
    importance: int | None = Field(default=None, ge=1, le=5)
    # `value` and `expires_at` may be set to null on purpose: the service reads
    # `model_fields_set` to tell a null that was sent from one that was not.
    expires_at: datetime | None = None

    @field_validator("value")
    @classmethod
    def _value_size(cls, value: JsonValue) -> JsonValue:
        return _small_json(value)


class MemoryOut(BaseModel):
    id: UUID
    scope: MemoryScope
    kind: MemoryKind
    key: str
    content: str
    value: JsonValue
    source_type: MemorySourceType
    confidence: float
    importance: int
    status: MemoryStatus
    owner_id: UUID
    team_id: UUID | None
    conversation_id: UUID | None
    source_message_id: UUID | None
    superseded_by_id: UUID | None
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    expires_at: datetime | None
    # Whether the caller may edit, approve, reject or delete it (decided server-side).
    editable: bool

    @classmethod
    def from_row(cls, row: Any, *, editable: bool) -> "MemoryOut":
        return cls(
            id=row.id,
            scope=row.scope,
            kind=row.kind,
            key=row.key,
            content=row.content,
            value=row.value,
            source_type=row.source_type,
            confidence=row.confidence,
            importance=row.importance,
            status=row.status,
            owner_id=row.owner_id,
            team_id=row.team_id,
            conversation_id=row.conversation_id,
            source_message_id=row.source_message_id,
            superseded_by_id=row.superseded_by_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_accessed_at=row.last_accessed_at,
            expires_at=row.expires_at,
            editable=editable,
        )


class SearchHit(BaseModel):
    memory: MemoryOut
    score: float


class MemorySettingsOut(BaseModel):
    extraction_enabled: bool
    auto_save: bool
    retention_days: int | None


class UpdateMemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_enabled: bool | None = None
    auto_save: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class ClearMemories(BaseModel):
    """Which of the caller's own memories to delete. Shared memories are never cleared here."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["all", "user", "conversation", "candidates"] = "all"
    conversation_id: UUID | None = None


class ClearResult(BaseModel):
    deleted: int
