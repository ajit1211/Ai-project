"""SQLAlchemy Core definitions of the tool tables (apps/core/nexa_core/tools/models.py) and of
the audit log Runtime appends tool events to (apps/core/nexa_core/audit/models.py).

Core owns the schema; Runtime writes invocations, approvals, organization tool settings and
audit rows (granted by `tools/0002_tenant_fks_and_runtime_grants`).
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Uuid,
)
from sqlalchemy.dialects.postgresql import INET, JSONB

metadata = MetaData()

tool_invocations = Table(
    "tool_invocations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("conversation_id", Uuid, nullable=False),
    Column("message_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("step", SmallInteger, nullable=False),
    Column("position", SmallInteger, nullable=False),
    Column("call_id", String, nullable=False),
    Column("tool_id", String, nullable=False),
    Column("tool_version", String, nullable=False),
    Column("status", String, nullable=False),
    Column("risk", String, nullable=False),
    Column("arguments", JSONB, nullable=False),
    Column("arguments_hash", String, nullable=False),
    Column("result", JSONB(none_as_null=True)),
    Column("result_truncated", Boolean, nullable=False),
    Column("error_code", String, nullable=False),
    Column("error_message", String, nullable=False),
    Column("sources", JSONB, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("attempts", SmallInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("duration_ms", Integer),
)

tool_approvals = Table(
    "tool_approvals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("invocation_id", Uuid, nullable=False),
    Column("requested_by_id", Uuid, nullable=False),
    Column("tool_id", String, nullable=False),
    Column("tool_version", String, nullable=False),
    Column("summary", String, nullable=False),
    Column("details", JSONB, nullable=False),
    Column("arguments_hash", String, nullable=False),
    Column("risk", String, nullable=False),
    Column("status", String, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_by_id", Uuid),
    Column("decided_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

tool_settings = Table(
    "tool_settings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid),
    Column("tool_id", String, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("updated_by_id", Uuid),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

audit_logs = Table(
    "audit_logs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid),
    Column("actor_type", String, nullable=False),
    Column("actor_id", Uuid),
    Column("action", String, nullable=False),
    Column("category", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("resource_type", String, nullable=False),
    Column("resource_id", String, nullable=False),
    Column("ip_address", INET),
    Column("user_agent", String, nullable=False),
    Column("request_id", String, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
