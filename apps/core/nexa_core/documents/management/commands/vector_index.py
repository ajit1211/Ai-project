"""Create or drop the approximate (HNSW) index for an embedding model's vectors.

    manage.py vector_index create [--model nexa-embed-small] [--m 16] [--ef-construction 64]
    manage.py vector_index drop   [--model nexa-embed-small]

Without it, semantic search is exact (every authorized vector is compared), which is the
right choice for small libraries. With it, search is approximate and faster on large ones;
tenant and document filters then apply to the index's candidates, so raise
RAG_HNSW_EF_SEARCH when filtered searches return too few results (see
docs/architecture/vector-search.md#indexes). Built CONCURRENTLY: writes continue meanwhile.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection

from nexa_core.ai.models import AIEmbeddingModel
from nexa_core.documents import embedding, vectors


class Command(BaseCommand):
    help = "Create or drop the HNSW index for one embedding model."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=["create", "drop"])
        parser.add_argument("--model", default="", help="Embedding model key (default: active).")
        parser.add_argument("--m", type=int, default=16)
        parser.add_argument("--ef-construction", type=int, default=64)

    def handle(self, *args: Any, **options: Any) -> None:
        if not vectors.status(connection).ready:
            raise CommandError("The vector store does not exist: run setup_vector_search.")
        model = (
            AIEmbeddingModel.objects.filter(key=options["model"]).first()
            if options["model"]
            else embedding.active_model()
        )
        if model is None:
            raise CommandError("No such embedding model.")
        if options["action"] == "drop":
            vectors.drop_hnsw_index(connection, model.key)
            self.stdout.write(f"Dropped {vectors.index_name(model.key)}; searches are exact.")
            return
        try:
            name = vectors.create_hnsw_index(
                connection,
                model_key=model.key,
                model_id=model.id,
                dimensions=model.dimensions,
                m=options["m"],
                ef_construction=options["ef_construction"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Created {name} for {model.key} ({model.dimensions} dimensions).")
