"""SQLAlchemy Core definitions of the Core-owned tables document-grounded answers use.

Core owns the schema (apps/core/nexa_core/documents/models.py, chat/models.py). Runtime only
reads the document tables; it writes `message_sources` alongside the replies it writes.
"""

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import TSVECTOR

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
