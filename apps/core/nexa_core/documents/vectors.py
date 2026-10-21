"""The vector store: `chunk_embeddings`, a pgvector table (docs/architecture/vector-search.md).

    chunk_embeddings
      id, organization_id, document_id   (composite key to documents: same tenant; deleting
                                          the document deletes its vectors)
      chunk_id                           (no foreign key: chunks are replaced on re-indexing,
                                          and vectors of unchanged chunks are kept and reused)
      embedding_model_id, space, dimensions, content_sha256
      embedding vector                   (one column for every model; each row's size is
                                          checked against `dimensions`)
      unique (chunk_id, embedding_model_id): one current vector per chunk and model

Only PostgreSQL with the pgvector extension can hold it. The extension needs a superuser
(scripts/db/init.sql creates it where the server has it installed); migrations run as the
application role, so they create the table when the extension is present and otherwise leave
it out: NEXA then runs with keyword search only, and reports semantic search as unavailable.
After installing pgvector later, `manage.py setup_vector_search` creates it.

Vectors cross the driver as pgvector's text form (`'[0.1,0.2,...]'::vector`), so no extra
Python package is needed. Nothing here logs a vector or a text.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db.backends.base.base import BaseDatabaseWrapper

from nexa_common.ids import new_id

TABLE = "chunk_embeddings"
# pgvector's HNSW and IVFFlat indexes support `vector` up to 2 000 dimensions.
MAX_INDEXED_DIMENSIONS = 2_000

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    document_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    embedding_model_id uuid NOT NULL
        REFERENCES ai_embedding_models (id) ON DELETE RESTRICT,
    space varchar(200) NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    content_sha256 varchar(64) NOT NULL,
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chunk_embeddings_dims_chk CHECK (vector_dims(embedding) = dimensions),
    CONSTRAINT chunk_embeddings_chunk_model_uniq UNIQUE (chunk_id, embedding_model_id),
    CONSTRAINT chunk_embeddings_document_same_org_fk FOREIGN KEY (organization_id, document_id)
        REFERENCES documents (organization_id, id) ON DELETE CASCADE
)
"""
CREATE_SCOPE_INDEX = (
    "CREATE INDEX IF NOT EXISTS chunk_embeddings_scope_idx "
    "ON chunk_embeddings (embedding_model_id, organization_id, document_id)"
)


@dataclass(frozen=True, slots=True)
class StoreStatus:
    extension_available: bool
    extension_installed: bool
    table_exists: bool

    @property
    def ready(self) -> bool:
        return self.extension_installed and self.table_exists


def status(connection: BaseDatabaseWrapper) -> StoreStatus:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT FROM pg_available_extensions WHERE name = 'vector'), "
            "EXISTS (SELECT FROM pg_extension WHERE extname = 'vector'), "
            "to_regclass('public.chunk_embeddings') IS NOT NULL"
        )
        available, installed, table = cursor.fetchone()
    return StoreStatus(bool(available), bool(installed), bool(table))


def ensure_store(connection: BaseDatabaseWrapper, *, create_extension: bool = False) -> str:
    """Create the table and its index when pgvector is installed. Returns what happened:
    "ready", "created", "extension_not_installed" or "extension_unavailable"."""
    current = status(connection)
    if not current.extension_installed:
        if not current.extension_available:
            return "extension_unavailable"
        if not create_extension:
            return "extension_not_installed"
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")  # needs a superuser
    if current.table_exists:
        return "ready"
    with connection.cursor() as cursor:
        cursor.execute(CREATE_TABLE)
        cursor.execute(CREATE_SCOPE_INDEX)
    return "created"


def literal(vector: Sequence[float]) -> str:
    """pgvector's text form. Seven significant digits: what a float4 column keeps anyway."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def existing(connection: BaseDatabaseWrapper, document_id: UUID, model_id: UUID) -> dict[UUID, str]:
    """Chunk id -> content hash of the vectors stored for this document and model."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, content_sha256 FROM chunk_embeddings "
            "WHERE document_id = %s AND embedding_model_id = %s",
            [document_id, model_id],
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


def upsert(
    connection: BaseDatabaseWrapper,
    *,
    organization_id: UUID,
    document_id: UUID,
    model_id: UUID,
    space: str,
    dimensions: int,
    items: Iterable[tuple[UUID, str, Sequence[float]]],
) -> int:
    """Store (chunk id, content hash, vector) rows; a chunk's previous vector for the same
    model is replaced, never duplicated."""
    rows: list[list[Any]] = [
        [
            new_id(),
            organization_id,
            document_id,
            chunk_id,
            model_id,
            space,
            dimensions,
            sha,
            literal(v),
        ]
        for chunk_id, sha, v in items
    ]
    if not rows:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO chunk_embeddings (id, organization_id, document_id, chunk_id, "
            "embedding_model_id, space, dimensions, content_sha256, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (chunk_id, embedding_model_id) DO UPDATE SET "
            "content_sha256 = EXCLUDED.content_sha256, embedding = EXCLUDED.embedding, "
            "space = EXCLUDED.space, dimensions = EXCLUDED.dimensions, created_at = now()",
            rows,
        )
    return len(rows)


def prune(
    connection: BaseDatabaseWrapper, document_id: UUID, model_id: UUID, keep: Sequence[UUID]
) -> int:
    """Delete this document's vectors for chunks that no longer exist."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM chunk_embeddings WHERE document_id = %s AND embedding_model_id = %s "
            "AND NOT (chunk_id = ANY(%s))",
            [document_id, model_id, list(keep)],
        )
        return int(cursor.rowcount)


def count_current(
    connection: BaseDatabaseWrapper, document_id: UUID, model_id: UUID, chunks: dict[UUID, str]
) -> int:
    """How many of the document's current chunks have a vector for their current text."""
    if not chunks:
        return 0
    stored = existing(connection, document_id, model_id)
    return sum(1 for chunk_id, sha in chunks.items() if stored.get(chunk_id) == sha)


def delete_document(connection: BaseDatabaseWrapper, document_id: UUID) -> int:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM chunk_embeddings WHERE document_id = %s", [document_id])
        return int(cursor.rowcount)


# --- Approximate index (optional, per model) ----------------------------------------------------


def index_name(model_key: str) -> str:
    return "chunk_embeddings_hnsw_" + model_key.replace("-", "_")


def create_hnsw_index(
    connection: BaseDatabaseWrapper,
    *,
    model_key: str,
    model_id: UUID,
    dimensions: int,
    m: int = 16,
    ef_construction: int = 64,
) -> str:
    """An HNSW index for one model's vectors (partial index on the model, expression index on
    the fixed size). Without it the search is exact, which is right for small collections.
    Runs CONCURRENTLY, so it must not be called inside a transaction."""
    if dimensions > MAX_INDEXED_DIMENSIONS:
        raise ValueError(
            f"pgvector cannot index {dimensions}-dimension vectors (at most "
            f"{MAX_INDEXED_DIMENSIONS}); searches stay exact."
        )
    name = index_name(model_key)
    # DDL takes no bind parameters: every interpolated value is an int or a UUID's canonical
    # text, and the name comes from a key validated as `nexa-embed-[a-z0-9-]+`.
    model = UUID(str(model_id))
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON chunk_embeddings "
            f"USING hnsw ((embedding::vector({int(dimensions)})) vector_cosine_ops) "
            f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)}) "
            f"WHERE embedding_model_id = '{model}'::uuid"
        )
    return name


def drop_hnsw_index(connection: BaseDatabaseWrapper, model_key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name(model_key)}")
