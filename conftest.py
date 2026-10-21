"""Repository-wide test fixtures."""

from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Flush with TRUNCATE ... CASCADE in tests.

    Tests that commit (`transaction=True`) are cleaned by Django's flush, which truncates the
    tables of Django's models. The pgvector table `chunk_embeddings` (Phase 09) is created by
    raw SQL where the extension exists, so Django does not know it, and PostgreSQL refuses to
    truncate `documents` while that table references it. CASCADE empties it with them. Test
    infrastructure only: nothing in the application flushes.
    """
    from django.db.backends.postgresql.operations import DatabaseOperations

    original = DatabaseOperations.sql_flush

    def sql_flush(
        self: DatabaseOperations,
        style: Any,
        tables: Any,
        *,
        reset_sequences: bool = False,
        allow_cascade: bool = False,
    ) -> list[str]:
        return original(self, style, tables, reset_sequences=reset_sequences, allow_cascade=True)

    DatabaseOperations.sql_flush = sql_flush  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _access_catalog(request: pytest.FixtureRequest) -> None:
    """Keep the permission catalog present for every database test.

    Migrations create the catalog once, but tests that commit (`transaction=True`) flush every
    table afterwards, catalog included. Restoring it here means test order never matters.
    """
    marker = request.node.get_closest_marker("django_db")
    if marker is None and not {"db", "transactional_db"} & set(request.fixturenames):
        return
    transactional = marker is not None and marker.kwargs.get("transaction", False)
    request.getfixturevalue("transactional_db" if transactional else "db")

    from nexa_core.ai.catalog import sync_ai_catalog, sync_embedding_models
    from nexa_core.ai.models import AIEmbeddingModel, AIModelAlias
    from nexa_core.organizations.models import Permission
    from nexa_core.organizations.roles import sync_access_catalog

    if not Permission.objects.exists():
        sync_access_catalog()
    if not AIModelAlias.objects.exists():
        sync_ai_catalog()
    if not AIEmbeddingModel.objects.exists():
        sync_embedding_models()
