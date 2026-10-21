"""Building the search index of a document (docs/architecture/rag.md#indexing).

    extraction completed, or a re-index request
        ─► request_indexing: generation + 1, status "pending", queued after the commit
        ─► core.documents.index(document_id, generation)
        ─► claim: row lock; a newer generation, a finished run or a live run means "skip"
        ─► chunk the stored text (chunking.py), outside any transaction
        ─► one transaction: delete the old chunks, insert the new ones, mark "indexed"
        └─► on failure: mark "failed"; the previous chunks, if any, stay searchable

Indexing reads only what Phase 07 stored (the extracted text and its page spans); the
original file is never parsed again. Nothing here logs document text: counts, ids, codes
and timings only.
"""

import hashlib
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from nexa_common.context import bind_request_context
from nexa_common.logs import get_logger
from nexa_core.documents import lifecycle, queue
from nexa_core.documents.chunking import (
    STRATEGY_VERSION,
    Chunk,
    ChunkingConfig,
    PageSpan,
    chunk_text,
)
from nexa_core.documents.models import (
    MAX_ERROR_MESSAGE_CHARS,
    Document,
    DocumentChunk,
    DocumentIndex,
    DocumentText,
)

log = get_logger("nexa.core.documents.indexing")

# Chunk ids are UUIDv5 names in this namespace (a fixed, arbitrary UUID).
CHUNK_NAMESPACE = UUID("6f4c2a1e-8d3b-5e7f-9a0c-1b2d3e4f5a6b")
_BATCH = 500


class IndexingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def chunk_id(document_id: UUID, chunk: Chunk) -> UUID:
    """Stable across re-indexing of unchanged text with the same strategy."""
    name = f"{document_id}:{STRATEGY_VERSION}:{chunk.index}:{chunk.content_sha256}"
    return uuid.uuid5(CHUNK_NAMESPACE, name)


def config_from_settings() -> ChunkingConfig:
    return ChunkingConfig(
        target_chars=settings.NEXA_RAG_CHUNK_TARGET_CHARS,
        max_chars=settings.NEXA_RAG_CHUNK_MAX_CHARS,
        overlap_chars=settings.NEXA_RAG_CHUNK_OVERLAP_CHARS,
        max_chunks=settings.NEXA_RAG_MAX_CHUNKS_PER_DOCUMENT,
    )


def timeout_seconds() -> int:
    return int(settings.NEXA_RAG_INDEX_TIMEOUT_SECONDS)


def is_running(index: DocumentIndex, *, now: datetime) -> bool:
    """An indexing run that started recently enough to still be alive."""
    if index.status != DocumentIndex.Status.INDEXING or index.started_at is None:
        return False
    return index.started_at > now - 2 * timedelta(seconds=timeout_seconds())


# --- Requesting ----------------------------------------------------------------------------------


def request_indexing(document: Document) -> DocumentIndex:
    """Queue a (re-)index of the document, inside the caller's transaction. A newer request
    supersedes an older one that has not finished: only the latest generation is stored."""
    now = timezone.now()
    index, _ = DocumentIndex.objects.select_for_update().get_or_create(
        document=document, defaults={"organization_id": document.organization_id}
    )
    index.generation += 1
    index.status = DocumentIndex.Status.PENDING
    index.requested_at = now
    index.error_code = ""
    index.error_message = ""
    index.save()
    document_id, generation = document.id, index.generation
    transaction.on_commit(lambda: dispatch_indexing(document_id, generation))
    return index


def dispatch_indexing(document_id: UUID, generation: int) -> None:
    from nexa_core.documents.tasks import index_document

    queue.dispatch(index_document, {"document_id": str(document_id), "generation": generation})


def forget_index(document: Document) -> None:
    """Remove a document's chunks and index state (inside the caller's transaction)."""
    DocumentChunk.objects.filter(document=document).delete()
    DocumentIndex.objects.filter(document=document).delete()


# --- Running -------------------------------------------------------------------------------------


def run_indexing(document_id: UUID, generation: int) -> str:
    """One indexing run. Returns "indexed", "failed", or why nothing was done: "superseded"
    (a newer request exists), "skipped" (already done or being done) or "cancelled"."""
    bind_request_context(document_id=str(document_id), index_generation=generation)
    started = time.perf_counter()
    claimed = _claim(document_id, generation)
    if isinstance(claimed, str):
        log.info("documents.indexing.skipped", reason=claimed)
        return claimed
    document = claimed
    bind_request_context(
        document_id=str(document_id),
        index_generation=generation,
        organization_id=str(document.organization_id),
    )
    try:
        chunks, warnings, source_sha256 = _chunks_for(document)
    except IndexingError as exc:
        return _fail(document_id, generation, exc.code, exc.message, started)
    except SoftTimeLimitExceeded:
        return _fail(document_id, generation, "timeout", "Indexing took too long.", started)
    except Exception as exc:
        log.error("documents.indexing.crashed", error=type(exc).__name__)
        return _fail(
            document_id,
            generation,
            "indexing_failed",
            "The document could not be indexed.",
            started,
        )
    return _store(document_id, generation, chunks, warnings, source_sha256, started)


def _lock(document_id: UUID) -> tuple[Document | None, DocumentIndex | None]:
    """Lock the document, then its index row: the order every writer uses (deleting,
    processing and re-index requests lock the document first), so they cannot deadlock."""
    document = Document.objects.select_for_update().filter(id=document_id).first()
    index = DocumentIndex.objects.select_for_update().filter(document_id=document_id).first()
    return document, index


def _claim(document_id: UUID, generation: int) -> Document | str:
    now = timezone.now()
    with transaction.atomic():
        document, index = _lock(document_id)
        if index is None or document is None:
            return "cancelled"  # the document was deleted, and its index with it
        if index.generation != generation:
            return "superseded"
        if index.status == DocumentIndex.Status.INDEXED and index.indexed_generation == generation:
            return "skipped"
        if is_running(index, now=now):
            return "skipped"
        if document.deleted_at is not None:
            return "cancelled"
        if document.status != lifecycle.COMPLETED:
            _mark_failed(index, "not_ready", "The document has no extracted text yet.", now)
            return "failed"
        index.status = DocumentIndex.Status.INDEXING
        index.started_at = now
        index.finished_at = None
        index.attempt_count += 1
        index.save(
            update_fields=["status", "started_at", "finished_at", "attempt_count", "updated_at"]
        )
        return document


def _chunks_for(document: Document) -> tuple[list[Chunk], list[str], str]:
    text = DocumentText.objects.filter(document_id=document.id).first()
    if text is None:
        raise IndexingError("no_text", "The document has no extracted text.")
    spans = [
        PageSpan(int(span["page"]), int(span["char_start"]), int(span["char_end"]))
        for span in text.pages or []
        if isinstance(span, dict) and {"page", "char_start", "char_end"} <= span.keys()
    ]
    result = chunk_text(
        text.content, fmt=document.format, pages=spans, config=config_from_settings()
    )
    if not result.chunks:
        raise IndexingError("no_text", "The document has no text to index.")
    digest = hashlib.sha256(text.content.encode("utf-8")).hexdigest()
    return result.chunks, result.warnings, digest


def _store(
    document_id: UUID,
    generation: int,
    chunks: list[Chunk],
    warnings: list[str],
    source_sha256: str,
    started: float,
) -> str:
    try:
        return _replace_chunks(document_id, generation, chunks, warnings, source_sha256, started)
    except Exception as exc:
        # Nothing was stored (the transaction rolled back); record why, so the run does not
        # stay "indexing" until the stalled-run sweep.
        log.error("documents.indexing.store_failed", error=type(exc).__name__)
        return _fail(
            document_id,
            generation,
            "indexing_failed",
            "The document could not be indexed.",
            started,
        )


def _replace_chunks(
    document_id: UUID,
    generation: int,
    chunks: list[Chunk],
    warnings: list[str],
    source_sha256: str,
    started: float,
) -> str:
    now = timezone.now()
    duration_ms = _elapsed_ms(started)
    with transaction.atomic():
        # Locking the document first also orders this run with a concurrent delete: either
        # the delete waits and then removes the chunks stored here, or this run waits and
        # finds the document deleted.
        document, index = _lock(document_id)
        if index is None or document is None or document.deleted_at is not None:
            return "cancelled"
        if index.generation != generation:
            return "superseded"  # the newer request rebuilds it
        DocumentChunk.objects.filter(document_id=document_id).delete()
        DocumentChunk.objects.bulk_create(
            [
                DocumentChunk(
                    id=chunk_id(document_id, chunk),
                    organization_id=document.organization_id,
                    document_id=document_id,
                    chunk_index=chunk.index,
                    kind=chunk.kind,
                    content=chunk.content,
                    section=chunk.section,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    row_start=chunk.row_start,
                    row_end=chunk.row_end,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    char_count=chunk.char_count,
                    token_estimate=chunk.token_estimate,
                    content_sha256=chunk.content_sha256,
                    strategy_version=STRATEGY_VERSION,
                )
                for chunk in chunks
            ],
            batch_size=_BATCH,
        )
        index.status = DocumentIndex.Status.INDEXED
        index.indexed_generation = generation
        index.strategy_version = STRATEGY_VERSION
        index.source_sha256 = source_sha256
        index.chunk_count = len(chunks)
        index.token_estimate = sum(chunk.token_estimate for chunk in chunks)
        index.warnings = warnings
        index.error_code = ""
        index.error_message = ""
        index.duration_ms = duration_ms
        index.finished_at = now
        index.indexed_at = now
        index.save()
    log.info(
        "documents.indexing.completed",
        chunks=len(chunks),
        token_estimate=index.token_estimate,
        warnings=len(warnings),
        duration_ms=duration_ms,
        strategy=STRATEGY_VERSION,
    )
    return "indexed"


def _fail(document_id: UUID, generation: int, code: str, message: str, started: float) -> str:
    with transaction.atomic():
        index = DocumentIndex.objects.select_for_update().filter(document_id=document_id).first()
        if index is None or index.generation != generation:
            return "superseded" if index is not None else "cancelled"
        index.duration_ms = _elapsed_ms(started)
        _mark_failed(index, code, message, timezone.now())
    log.warning("documents.indexing.failed", error_code=code, duration_ms=index.duration_ms)
    return "failed"


def _mark_failed(index: DocumentIndex, code: str, message: str, now: datetime) -> None:
    index.status = DocumentIndex.Status.FAILED
    index.error_code = code
    index.error_message = message[:MAX_ERROR_MESSAGE_CHARS]
    index.finished_at = now
    index.save()


def fail_stalled(now: datetime) -> int:
    """Mark runs whose worker died as failed, so they can be requested again."""
    cutoff = now - 2 * timedelta(seconds=timeout_seconds())
    stalled = 0
    for index in DocumentIndex.objects.filter(
        status=DocumentIndex.Status.INDEXING, started_at__lt=cutoff
    ):
        with transaction.atomic():
            locked = DocumentIndex.objects.select_for_update().get(pk=index.pk)
            if locked.status != DocumentIndex.Status.INDEXING:
                continue
            _mark_failed(
                locked, "timeout", "Indexing did not finish; it can be requested again.", now
            )
            stalled += 1
    return stalled


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


# --- Reporting -----------------------------------------------------------------------------------


def index_payload(index: DocumentIndex | None) -> dict[str, Any]:
    """The index state as the API shows it. `stale`: built by an older chunking strategy;
    still searchable, and rebuilt by a re-index."""
    if index is None:
        return {
            "status": "none",
            "searchable": False,
            "stale": False,
            "chunk_count": 0,
            "token_estimate": 0,
            "strategy_version": None,
            "attempt_count": 0,
            "warnings": [],
            "error_code": None,
            "error_message": None,
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
            "indexed_at": None,
        }
    stale = index.searchable and index.strategy_version != STRATEGY_VERSION
    status = "stale" if stale and index.status == DocumentIndex.Status.INDEXED else index.status
    return {
        "status": status,
        "searchable": index.searchable,
        "stale": stale,
        "chunk_count": index.chunk_count,
        "token_estimate": index.token_estimate,
        "strategy_version": index.strategy_version or None,
        "attempt_count": index.attempt_count,
        "warnings": list(index.warnings or []),
        "error_code": index.error_code or None,
        "error_message": index.error_message or None,
        "requested_at": _iso(index.requested_at),
        "started_at": _iso(index.started_at),
        "finished_at": _iso(index.finished_at),
        "indexed_at": _iso(index.indexed_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
