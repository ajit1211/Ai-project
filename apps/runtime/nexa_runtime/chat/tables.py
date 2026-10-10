"""SQLAlchemy Core definitions of the chat tables (apps/core/nexa_core/chat/models.py).

Core owns the schema; Runtime is the writer of both tables.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

conversations = Table(
    "conversations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("owner_id", Uuid, nullable=False),
    Column("title", String, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("status", String, nullable=False),
    Column("message_count", Integer, nullable=False),
    Column("active_generation_id", Uuid),
    Column("generation_started_at", DateTime(timezone=True)),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_message_at", DateTime(timezone=True)),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("conversation_id", Uuid, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("role", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("client_message_id", String, nullable=False),
    Column("model", String, nullable=False),
    Column("provider", String, nullable=False),
    Column("provider_model", String, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_micros", BigInteger),
    Column("latency_ms", Integer, nullable=False),
    Column("ttft_ms", Integer),
    Column("usage_event_id", Uuid),
    Column("request_id", String, nullable=False),
    Column("finish_reason", String, nullable=False),
    Column("error_code", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)
