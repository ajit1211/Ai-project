"""SQLAlchemy Core definitions of the memory tables (apps/core/nexa_core/memory/models.py).

Core owns the schema; Runtime writes all four tables. `memories.search` is a generated
column: never written, only matched.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

metadata = MetaData()

memories = Table(
    "memories",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("owner_id", Uuid, nullable=False),
    Column("team_id", Uuid),
    Column("conversation_id", Uuid),
    Column("source_message_id", Uuid),
    Column("scope", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("key", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("value", JSONB),
    Column("source_type", String, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("importance", SmallInteger, nullable=False),
    Column("status", String, nullable=False),
    Column("superseded_by_id", Uuid),
    Column("content_hash", String, nullable=False),
    Column("search", TSVECTOR),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_accessed_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
)

memory_settings = Table(
    "memory_settings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("extraction_enabled", Boolean, nullable=False),
    Column("auto_save", Boolean, nullable=False),
    Column("retention_days", SmallInteger),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

conversation_summaries = Table(
    "conversation_summaries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("conversation_id", Uuid, nullable=False),
    Column("version", Integer, nullable=False),
    Column("through_sequence", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("token_estimate", Integer, nullable=False),
    Column("model", String, nullable=False),
    Column("usage_event_id", Uuid),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

memory_extractions = Table(
    "memory_extractions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("conversation_id", Uuid, nullable=False),
    Column("message_id", Uuid, nullable=False),
    Column("status", String, nullable=False),
    Column("candidates_found", SmallInteger, nullable=False),
    Column("stored", SmallInteger, nullable=False),
    Column("rejected", SmallInteger, nullable=False),
    Column("duplicates", SmallInteger, nullable=False),
    Column("error_code", String, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("usage_event_id", Uuid),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)
