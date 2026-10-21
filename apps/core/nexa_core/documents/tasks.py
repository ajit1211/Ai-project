"""Processing documents in the worker (docs/architecture/documents.md#processing).

    core.documents.process(document_id, ingestion_id)
        claim the attempt ──► copy the bytes to a temporary file ──► extract
        ──► store the text and the outcome ──► log

The task reads everything by id, claims the attempt with a row lock so a redelivered or
duplicated task does nothing, writes outcomes to the document and its attempt (there is no
result backend), and verifies the document is still there before writing. The worker uses
the organization and owner stored on the document; the uploader's session is not involved.
A completed extraction queues `core.documents.index` (indexing.py), which builds the search
index used for document-grounded answers. `core.documents.purge` is the retention task; it
is scheduled by operations.
"""

import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from nexa_common.context import bind_request_context
from nexa_common.logs import get_logger
from nexa_core.documents import embedding, indexing, lifecycle
from nexa_core.documents.extraction import (
    PARSER_VERSION,
    Extraction,
    ExtractionError,
    Format,
    Limits,
    extract,
)
from nexa_core.documents.models import Document, DocumentIngestion, DocumentText
from nexa_core.documents.services import remove_file
from nexa_core.documents.storage import StorageError, get_storage, read_chunks

log = get_logger("nexa.core.documents.tasks")

TIMEOUT = int(settings.NEXA_DOCUMENTS_PROCESSING_TIMEOUT_SECONDS)
# Codes that mean "the file is what it is", not "something went wrong".
_OUTCOMES: dict[str, str] = {
    "needs_ocr": lifecycle.NEEDS_OCR,
    "unsupported_content": lifecycle.UNSUPPORTED,
    "unsupported_format": lifecycle.UNSUPPORTED,
}


def limits_from_settings() -> Limits:
    return Limits(
        max_text_chars=settings.NEXA_DOCUMENTS_MAX_TEXT_CHARS,
        max_pages=settings.NEXA_DOCUMENTS_MAX_PAGES,
        max_csv_rows=settings.NEXA_DOCUMENTS_MAX_CSV_ROWS,
    )


@shared_task(
    name="core.documents.process",
    soft_time_limit=max(30, TIMEOUT - 30),
    time_limit=TIMEOUT,
)
def process_document(document_id: str, ingestion_id: str) -> str:
    return run_processing(UUID(document_id), UUID(ingestion_id))


def run_processing(document_id: UUID, ingestion_id: UUID) -> str:
    """One attempt, start to finish. Returns the outcome: a status, "skipped" for a claim
    that was already taken, or "cancelled" for a document deleted meanwhile."""
    bind_request_context(document_id=str(document_id), ingestion_id=str(ingestion_id))
    started = time.perf_counter()
    with transaction.atomic():
        ingestion = (
            DocumentIngestion.objects.select_for_update(skip_locked=True)
            .select_related("document")
            .filter(id=ingestion_id, document_id=document_id)
            .first()
        )
        if ingestion is None or ingestion.status != lifecycle.QUEUED:
            log.info("documents.processing.skipped", reason="not_queued")
            return "skipped"
        document = ingestion.document
        if document.deleted_at is not None:
            _cancel(ingestion)
            return lifecycle.CANCELLED
        now = timezone.now()
        ingestion.status = lifecycle.PROCESSING
        ingestion.started_at = now
        ingestion.parser_version = PARSER_VERSION
        ingestion.save(update_fields=["status", "started_at", "parser_version"])
        lifecycle.transition(document, lifecycle.PROCESSING, now=now)
        document.save(
            update_fields=["status", "processing_started_at", "processed_at", "updated_at"]
        )
        bind_request_context(
            document_id=str(document_id),
            ingestion_id=str(ingestion_id),
            organization_id=str(document.organization_id),
        )
    log.info(
        "documents.processing.started",
        format=document.format,
        size_bytes=document.size_bytes,
        attempt=ingestion.attempt,
    )

    outcome, extraction, error = _extract(document)
    duration_ms = int((time.perf_counter() - started) * 1000)

    with transaction.atomic():
        document = Document.objects.select_for_update().get(id=document_id)
        ingestion = DocumentIngestion.objects.select_for_update().get(id=ingestion_id)
        if document.deleted_at is not None or ingestion.status != lifecycle.PROCESSING:
            _cancel(ingestion)
            return lifecycle.CANCELLED
        now = timezone.now()
        if outcome == lifecycle.COMPLETED and extraction is not None:
            DocumentText.objects.update_or_create(
                document=document,
                defaults={
                    "content": extraction.text,
                    "pages": [
                        {"page": s.page, "char_start": s.char_start, "char_end": s.char_end}
                        for s in extraction.pages
                    ],
                    "char_count": extraction.char_count,
                    "parser_version": PARSER_VERSION,
                },
            )
            document.page_count = extraction.page_count
            document.word_count = extraction.word_count
            document.char_count = extraction.char_count
            document.metadata = extraction.metadata
            document.warnings = extraction.warnings
            document.error_code = ""
            document.error_message = ""
            ingestion.page_count = extraction.page_count
            ingestion.word_count = extraction.word_count
            ingestion.char_count = extraction.char_count
            ingestion.warnings = extraction.warnings
        else:
            code, message = error or ("extraction_failed", "The file could not be processed.")
            document.error_code = code
            document.error_message = message
            ingestion.error_code = code
            ingestion.error_message = message
        document.parser_version = PARSER_VERSION
        lifecycle.transition(document, outcome, now=now)
        document.save()
        ingestion.status = outcome
        ingestion.duration_ms = duration_ms
        ingestion.finished_at = now
        ingestion.save()
        if outcome == lifecycle.COMPLETED:
            indexing.request_indexing(document)  # queued once this transaction commits

    log.info(
        "documents.processing.completed",
        outcome=outcome,
        error_code=document.error_code or None,
        format=document.format,
        size_bytes=document.size_bytes,
        attempt=ingestion.attempt,
        duration_ms=duration_ms,
        page_count=document.page_count,
        char_count=document.char_count,
        warnings=len(document.warnings or []),
    )
    return outcome


def _cancel(ingestion: DocumentIngestion) -> None:
    ingestion.status = lifecycle.CANCELLED
    ingestion.finished_at = timezone.now()
    ingestion.save(update_fields=["status", "finished_at"])
    log.info("documents.processing.cancelled")


def _extract(
    document: Document,
) -> tuple[str, Extraction | None, tuple[str, str] | None]:
    """Run the engine on a private copy of the bytes. Never raises: every failure becomes
    an outcome with a code and a message safe to show."""
    fmt: Format = document.format  # type: ignore[assignment]
    suffix = Path(document.filename).suffix.lower() or f".{fmt}"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temporary = Path(handle.name)
            with get_storage().open(document.storage_key) as stored:
                for chunk in read_chunks(stored):
                    handle.write(chunk)
        extraction = extract(temporary, fmt, limits_from_settings())
    except StorageError as exc:
        log.error("documents.storage.read_failed", error=type(exc).__name__)
        return (
            lifecycle.FAILED,
            None,
            ("storage_unavailable", "The file could not be read from storage."),
        )
    except ExtractionError as exc:
        return _OUTCOMES.get(exc.code, lifecycle.FAILED), None, (exc.code, exc.message)
    except SoftTimeLimitExceeded:
        return lifecycle.FAILED, None, ("timeout", "Processing took too long and was stopped.")
    except Exception as exc:
        log.error("documents.processing.crashed", error=type(exc).__name__)
        return lifecycle.FAILED, None, ("extraction_failed", "The file could not be processed.")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.warning("documents.temporary.cleanup_failed")
    return lifecycle.COMPLETED, extraction, None


# --- Retention -------------------------------------------------------------------------------


def purge_documents() -> dict[str, int]:
    """Finish deletions and tidy stalled attempts:

    1. remove the bytes of deleted documents whose removal failed earlier;
    2. hard-delete deleted documents once their bytes are gone and the grace period passed;
    3. mark attempts that stalled (a worker died mid-way) as failed, so they can be retried;
    4. the same for indexing runs.
    """
    now = timezone.now()
    files_removed = sum(
        1
        for document in Document.objects.filter(
            deleted_at__isnull=False, file_deleted_at__isnull=True
        )
        if remove_file(document)
    )
    grace = timedelta(days=settings.NEXA_DOCUMENTS_PURGE_AFTER_DAYS)
    # `delete()` counts the cascaded rows too (attempts, text): report documents only.
    _, deleted = Document.objects.filter(
        deleted_at__lt=now - grace, file_deleted_at__isnull=False
    ).delete()
    purged = deleted.get(Document._meta.label, 0)
    stale_cutoff = now - 3 * timedelta(seconds=TIMEOUT)
    stalled = 0
    for document in Document.objects.filter(
        status__in=list(lifecycle.IN_FLIGHT), updated_at__lt=stale_cutoff, deleted_at__isnull=True
    ):
        with transaction.atomic():
            document = Document.objects.select_for_update().get(id=document.id)
            if document.status not in lifecycle.IN_FLIGHT:
                continue
            document.error_code = "timeout"
            document.error_message = "Processing did not finish; it can be tried again."
            lifecycle.transition(document, lifecycle.FAILED, now=now)
            document.save()
            DocumentIngestion.objects.filter(
                document=document, status__in=list(lifecycle.IN_FLIGHT)
            ).update(
                status=lifecycle.FAILED,
                error_code="timeout",
                error_message=document.error_message,
                finished_at=now,
            )
            stalled += 1
    counts: dict[str, Any] = {
        "files_removed": files_removed,
        "purged": purged,
        "stalled": stalled,
        "indexes_stalled": indexing.fail_stalled(now),
        "embeddings_stalled": embedding.fail_stalled(now),
    }
    log.info("documents.purge.completed", **counts)
    return counts


@shared_task(name="core.documents.purge")
def purge() -> dict[str, int]:
    return purge_documents()


# --- Search index (Phase 08) -----------------------------------------------------------------

INDEX_TIMEOUT = indexing.timeout_seconds()


@shared_task(
    name="core.documents.index",
    soft_time_limit=max(30, INDEX_TIMEOUT - 30),
    time_limit=INDEX_TIMEOUT,
)
def index_document(document_id: str, generation: int) -> str:
    return indexing.run_indexing(UUID(document_id), int(generation))


# --- Semantic index (Phase 09) -------------------------------------------------------------------

EMBED_TIMEOUT = embedding.job_timeout()


@shared_task(
    bind=True,
    name="core.documents.embed",
    soft_time_limit=max(30, EMBED_TIMEOUT - 30),
    time_limit=EMBED_TIMEOUT,
    max_retries=settings.NEXA_EMBEDDING_TASK_RETRIES,
)
def embed_document(
    self: Any, document_id: str, embedding_model_id: str, generation: int, force: bool = False
) -> str:
    outcome = embedding.run_embedding(
        UUID(document_id), UUID(embedding_model_id), int(generation), force=bool(force)
    )
    if outcome == "retry" and not self.request.is_eager and self.request.retries < self.max_retries:
        # A provider outage or rate limit outlasted the embedder's own retries: try the whole
        # run again later. Finished batches are kept and reused.
        raise self.retry(countdown=60 * (self.request.retries + 1))
    return outcome
