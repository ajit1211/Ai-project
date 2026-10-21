"""Queue embedding of indexed documents for the active embedding model.

    manage.py embed_documents                    # documents not yet ready for this model
    manage.py embed_documents --force            # every indexed document, all passages again
    manage.py embed_documents --organization <id> --dry-run

Used after enabling semantic search on an existing library, and after switching
EMBEDDING_MODEL (a new model is a new vector space: every document needs its vectors).
Unchanged passages keep their vectors unless --force. Work runs in the Celery worker.
"""

from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import F

from nexa_core.documents import embedding, lifecycle
from nexa_core.documents.models import Document, DocumentEmbedding


class Command(BaseCommand):
    help = "Queue embedding of indexed documents for the active embedding model."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", type=UUID, default=None)
        parser.add_argument("--force", action="store_true", help="Embed every passage again.")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        status = embedding.availability(fresh=True)
        if not status.enabled or status.model is None:
            raise CommandError(f"Semantic search is not available here: {status.reason}.")
        model = status.model
        documents = Document.objects.filter(
            deleted_at__isnull=True,
            status=lifecycle.COMPLETED,
            search_index__indexed_at__isnull=False,
            search_index__chunk_count__gt=0,
        ).order_by("id")
        if options["organization"]:
            documents = documents.filter(organization_id=options["organization"])
        if not options["force"]:
            ready = DocumentEmbedding.objects.filter(
                embedding_model=model,
                status=DocumentEmbedding.Status.READY,
                source_generation=F("document__search_index__indexed_generation"),
            ).values("document_id")
            documents = documents.exclude(id__in=ready)
        if options["limit"]:
            documents = documents[: options["limit"]]
        ids = list(documents.values_list("id", flat=True))
        if options["dry_run"]:
            self.stdout.write(f"Would queue {len(ids)} documents for {model.key}.")
            return
        queued = 0
        for document_id in ids:
            with transaction.atomic():
                document = Document.objects.select_for_update().get(id=document_id)
                if document.deleted_at is None and embedding.request_embedding(
                    document, force=options["force"]
                ):
                    queued += 1
        self.stdout.write(f"Queued {queued} documents for {model.key}.")
