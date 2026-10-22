"""SQLAlchemy Core definitions of the Core-owned tables document-grounded answers use.

Core owns the schema (apps/core/nexa_core/documents/models.py, chat/models.py, ai/models.py and
the pgvector table in documents/vectors.py). Runtime only reads the document, index, embedding
and model tables; it writes `message_sources` alongside the replies it writes.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """pgvector's `vector` (optionally sized). Values are sent as text and cast on the server,
    so no driver codec is needed; vectors are never read back."""

    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: object) -> str:
        return f"vector({self.dimensions})" if self.dimensions else "vector"


metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("uploaded_by_id", Uuid),
    Column("team_id", Uuid),
    Column("visibility", String, nullable=False),
    Column("filename", String, nullable=False),
    Column("title", String, nullable=False),
    Column("format", String, nullable=False),
    Column("status", String, nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    # Metadata the document tools may show (Phase 10).
    Column("size_bytes", BigInteger),
    Column("page_count", Integer),
    Column("word_count", Integer),
    Column("created_at", DateTime(timezone=True)),
)

teams = Table(
    "teams",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
)

document_indexes = Table(
    "document_indexes",
    metadata,
    Column("document_id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("status", String, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("indexed_generation", Integer),
    Column("indexed_at", DateTime(timezone=True)),
)

document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("section", String, nullable=False),
    Column("page_start", Integer),
    Column("page_end", Integer),
    Column("row_start", Integer),
    Column("row_end", Integer),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("token_estimate", Integer, nullable=False),
    Column("content_sha256", String, nullable=False),
    Column("search", TSVECTOR),
)

message_sources = Table(
    "message_sources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("message_id", Uuid, nullable=False),
    Column("document_id", Uuid),
    Column("chunk_id", Uuid, nullable=False),
    Column("label", String, nullable=False),
    Column("rank", Integer, nullable=False),
    Column("score", Float, nullable=False),
    Column("cited", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


ai_embedding_models = Table(
    "ai_embedding_models",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", String, nullable=False),
    Column("provider", String, nullable=False),
    Column("provider_model", String, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("version", String, nullable=False),
    Column("distance", String, nullable=False),
    Column("max_batch_size", Integer, nullable=False),
    Column("max_input_tokens", Integer, nullable=False),
    Column("input_micros_per_million", Integer),
    Column("relevance_floor", Float),
    Column("is_active", Boolean, nullable=False),
    Column("is_default", Boolean, nullable=False),
    Column("use_cases", JSONB, nullable=False),
)

document_embeddings = Table(
    "document_embeddings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("embedding_model_id", Uuid, nullable=False),
    Column("status", String, nullable=False),
    Column("source_generation", Integer),
)

# Created only where pgvector exists (Core's documents/vectors.py).
chunk_embeddings = Table(
    "chunk_embeddings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("chunk_id", Uuid, nullable=False),
    Column("embedding_model_id", Uuid, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("content_sha256", String, nullable=False),
    Column("embedding", Vector(), nullable=False),
)
