"""SQLAlchemy Core definitions of the Core-owned AI tables (apps/core/nexa_core/ai/models.py).

Runtime reads the catalog and policies and inserts usage events; it writes nothing else.
"""

from sqlalchemy import (
    ARRAY,
    BigInteger,
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

metadata = MetaData()

ai_models = Table(
    "ai_models",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider", String, nullable=False),
    Column("provider_model", String, nullable=False),
    Column("display_name", String, nullable=False),
    Column("capabilities", ARRAY(String), nullable=False),
    Column("context_window", Integer, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("accepts_temperature", Boolean, nullable=False),
    Column("is_active", Boolean, nullable=False),
)

ai_model_prices = Table(
    "ai_model_prices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("model_id", Uuid, nullable=False),
    Column("input_micros_per_million", BigInteger, nullable=False),
    Column("output_micros_per_million", BigInteger, nullable=False),
    Column("cached_input_micros_per_million", BigInteger, nullable=False),
    Column("cache_write_micros_per_million", BigInteger, nullable=False),
    Column("effective_from", DateTime(timezone=True), nullable=False),
)

ai_model_aliases = Table(
    "ai_model_aliases",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String, nullable=False),
    Column("display_name", String, nullable=False),
    Column("description", String, nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("is_default", Boolean, nullable=False),
    Column("available_by_default", Boolean, nullable=False),
)

ai_alias_targets = Table(
    "ai_alias_targets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("alias_id", Uuid, nullable=False),
    Column("model_id", Uuid, nullable=False),
    Column("priority", SmallInteger, nullable=False),
)

ai_organization_policies = Table(
    "ai_organization_policies",
    metadata,
    Column("organization_id", Uuid, primary_key=True),
    Column("ai_enabled", Boolean, nullable=False),
    Column("requests_per_minute", Integer),
    Column("requests_per_day", Integer),
    Column("tokens_per_day", BigInteger),
    Column("max_output_tokens", Integer),
    Column("concurrent_requests", SmallInteger),
)

ai_organization_alias_grants = Table(
    "ai_organization_alias_grants",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("alias_id", Uuid, nullable=False),
)

ai_usage_events = Table(
    "ai_usage_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("organization_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("actor_type", String, nullable=False),
    Column("request_id", String, nullable=False),
    Column("source_type", String),
    Column("source_id", Uuid),
    Column("feature", String, nullable=False),
    Column("alias", String, nullable=False),
    Column("provider", String, nullable=False),
    Column("provider_model", String, nullable=False),
    Column("model_id", Uuid),
    Column("price_id", Uuid),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cached_input_tokens", Integer, nullable=False),
    Column("cache_write_input_tokens", Integer, nullable=False),
    Column("total_tokens", Integer, nullable=False),
    Column("cost_micros", BigInteger),
    Column("latency_ms", Integer, nullable=False),
    Column("ttft_ms", Integer),
    Column("status", String, nullable=False),
    Column("error_code", String),
    Column("attempts", SmallInteger, nullable=False),
    Column("fallback_used", Boolean, nullable=False),
    Column("stream", Boolean, nullable=False),
    Column("structured", Boolean, nullable=False),
    Column("usage_source", String, nullable=False),
)
