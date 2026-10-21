"""The vector store, `chunk_embeddings` (Phase 09, docs/architecture/vector-search.md).

Created only where the pgvector extension is installed. The extension itself needs a superuser
(scripts/db/init.sql creates it where the server has it); this migration runs as the
application role, so where pgvector is missing it leaves the table out and NEXA keeps working
with keyword search only. `manage.py setup_vector_search` creates the table later, once
pgvector is installed. The SQL is copied here (not imported) so the migration never changes.
"""

from typing import Any

from django.db import migrations

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
CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS chunk_embeddings_scope_idx "
    "ON chunk_embeddings (embedding_model_id, organization_id, document_id)"
)


def create_store(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector')")
        if not cursor.fetchone()[0]:
            return  # keyword search only until pgvector is installed
        cursor.execute(CREATE_TABLE)
        cursor.execute(CREATE_INDEX)


def drop_store(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS chunk_embeddings")


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0004_documentembedding"),
        ("ai", "0003_aiembeddingmodel"),
    ]

    operations = [migrations.RunPython(create_store, drop_store)]
