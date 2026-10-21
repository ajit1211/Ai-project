"""Create the vector store once pgvector is installed, and report whether semantic search runs.

    uv run python apps/core/manage.py setup_vector_search
    uv run python apps/core/manage.py setup_vector_search --create-extension   # as a superuser

Safe to run more than once. The migration creates the table where the extension already
exists; this command is for a database where pgvector was installed afterwards.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection

from nexa_core.documents import embedding, vectors


class Command(BaseCommand):
    help = "Create the pgvector table for semantic search and report its availability."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--create-extension",
            action="store_true",
            help="Also run CREATE EXTENSION vector (needs a PostgreSQL superuser).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        outcome = vectors.ensure_store(connection, create_extension=options["create_extension"])
        messages = {
            "ready": "The vector store already exists.",
            "created": "Created the vector store (chunk_embeddings).",
            "extension_not_installed": (
                "pgvector is available on this server but not installed in this database. "
                "As a superuser run: CREATE EXTENSION vector; then run this command again."
            ),
            "extension_unavailable": (
                "pgvector is not installed on this PostgreSQL server. See "
                "docs/architecture/vector-search.md#installing-pgvector."
            ),
        }
        self.stdout.write(messages[outcome])
        status = embedding.availability(fresh=True)
        model = status.model.key if status.model else "none"
        self.stdout.write(f"Semantic search: {status.reason} (embedding model: {model}).")
        if status.enabled:
            self.stdout.write("Embed existing documents with: manage.py embed_documents")
