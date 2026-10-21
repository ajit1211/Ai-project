"""Embedding a document's chunks (Phase 09, docs/architecture/vector-search.md#indexing).

    search index stored (indexing.py) ─► request_embedding: generation + 1, "pending"
        ─► after commit: core.documents.embed(document, model, generation)
        ─► claim: document, then index, then state row locked; a newer generation, a run in
           progress or a finished run for these chunks means "skip"
        ─► chunks whose text already has a vector for this model are reused
        ─► the rest, in batches within the model's limits, through nexa_ai's Embedder
           (retries, timeouts, validation, unit length); each batch is stored as it arrives,
           so a failure halfway keeps the finished batches for the next attempt
        ─► finalize: vectors of chunks that no longer exist are removed; "ready" only when
           every current chunk has a vector for its current text

Only processed, indexed, undeleted documents are embedded, by the worker (never in a request
handler, except through the queue's broker-down fallback). Each provider call is recorded in
the AI usage ledger (feature "embedding"), attributed to the document's organization and
owner. Nothing here logs chunk text or vectors.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from nexa_ai.embeddings import (
    Embedder,
    EmbeddingModel,
    Embeddings,
    build_embedding_providers,
    choose_model,
)
from nexa_ai.errors import AIError, AITimeout
from nexa_ai.tokens import estimate_text_tokens
from nexa_common.context import bind_request_context
from nexa_common.ids import new_id
from nexa_common.logs import get_logger
from nexa_core.ai.models import AIEmbeddingModel, AIUsageEvent
from nexa_core.documents import lifecycle, queue, vectors
from nexa_core.documents.models import (
    MAX_ERROR_MESSAGE_CHARS,
    Document,
    DocumentChunk,
    DocumentEmbedding,
    DocumentIndex,
)

log = get_logger("nexa.core.documents.embedding")

NIL_USER = UUID(int=0)


# --- Which model, and whether embedding can run here ---------------------------------------------


def spec(row: AIEmbeddingModel) -> EmbeddingModel:
    return EmbeddingModel(
        key=row.key,
        provider=row.provider,
        provider_model=row.provider_model,
        dimensions=row.dimensions,
        version=row.version,
        distance=row.distance,
        max_batch_size=row.max_batch_size,
        max_input_tokens=row.max_input_tokens,
        price_micros_per_million=row.input_micros_per_million,
        relevance_floor=row.relevance_floor,
        is_default=row.is_default,
        id=row.id,
    )


def available_providers() -> set[str]:
    providers = set()
    if settings.NEXA_AI_OPENAI_API_KEY:
        providers.add("openai")
    if settings.NEXA_EMBEDDING_MOCK_ALLOWED:
        providers.add("mock")
    return providers


def active_model() -> AIEmbeddingModel | None:
    """The model new vectors are made with (the shared rule in nexa_ai.embeddings.registry)."""
    rows = list(AIEmbeddingModel.objects.filter(is_active=True))
    chosen = choose_model(
        [spec(row) for row in rows],
        configured=settings.NEXA_EMBEDDING_MODEL,
        available_providers=available_providers(),
        mock_allowed=settings.NEXA_EMBEDDING_MOCK_ALLOWED,
    )
    return next((row for row in rows if chosen is not None and row.key == chosen.key), None)


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether documents can be embedded now, and if not, why (a stable code)."""

    reason: str  # ok | disabled | pgvector_missing | no_model | provider_not_configured
    model: AIEmbeddingModel | None = None

    @property
    def enabled(self) -> bool:
        return self.reason == "ok"


_AVAILABILITY_TTL = 15.0
_cached: tuple[tuple[Any, ...], float, Availability] | None = None


def availability(*, fresh: bool = False) -> Availability:
    """Cached for a few seconds per process (a library page asks once per document), keyed on
    the settings it depends on; `fresh` bypasses the cache (the worker always does)."""
    global _cached
    key = (
        settings.NEXA_EMBEDDING_ENABLED,
        settings.NEXA_EMBEDDING_MODEL,
        bool(settings.NEXA_AI_OPENAI_API_KEY),
        settings.NEXA_EMBEDDING_MOCK_ALLOWED,
    )
    now = time.monotonic()
    if (
        not fresh
        and _cached is not None
        and _cached[0] == key
        and now - _cached[1] < _AVAILABILITY_TTL
    ):
        return _cached[2]
    result = _availability()
    _cached = (key, now, result)
    return result


def _availability() -> Availability:
    if not settings.NEXA_EMBEDDING_ENABLED:
        return Availability("disabled")
    if not vectors.status(connection).ready:
        return Availability("pgvector_missing")
    model = active_model()
    if model is None:
        return Availability("no_model")
    if model.provider not in available_providers():
        return Availability("provider_not_configured", model)
    return Availability("ok", model)


# --- Requesting ----------------------------------------------------------------------------------


def request_embedding(document: Document, *, force: bool = False) -> DocumentEmbedding | None:
    """Queue embedding of the document's current chunks for the active model, inside the
    caller's transaction. Does nothing when embedding cannot run here (see `availability`)."""
    status = availability(fresh=True)
    if not status.enabled or status.model is None:
        return None
    model = status.model
    now = timezone.now()
    state, _ = DocumentEmbedding.objects.select_for_update().get_or_create(
        document=document,
        embedding_model=model,
        defaults={"organization_id": document.organization_id, "space": model.space},
    )
    state.generation += 1
    state.status = DocumentEmbedding.Status.PENDING
    state.requested_at = now
    state.error_code = ""
    state.error_message = ""
    state.retryable = False
    state.save()
    document_id, model_id, generation = document.id, model.id, state.generation
    transaction.on_commit(lambda: dispatch_embedding(document_id, model_id, generation, force))
    return state


def dispatch_embedding(document_id: UUID, model_id: UUID, generation: int, force: bool) -> None:
    from nexa_core.documents.tasks import embed_document

    queue.dispatch(
        embed_document,
        {
            "document_id": str(document_id),
            "embedding_model_id": str(model_id),
            "generation": generation,
            "force": force,
        },
    )


def forget_embeddings(document: Document) -> None:
    """Remove a document's vectors and embedding states (inside the caller's transaction)."""
    if vectors.status(connection).table_exists:
        vectors.delete_document(connection, document.id)
    DocumentEmbedding.objects.filter(document=document).delete()


# --- Running -------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Claim:
    document: Document
    index: DocumentIndex
    state: DocumentEmbedding
    model: AIEmbeddingModel


def run_embedding(
    document_id: UUID, model_id: UUID, generation: int, *, force: bool = False
) -> str:
    """One embedding run. Returns "ready", "failed", "retry" (a retryable failure: the task
    tries again later) or why nothing was done: "superseded", "skipped", "cancelled",
    "unavailable"."""
    bind_request_context(document_id=str(document_id), embedding_generation=generation)
    started = time.perf_counter()
    if not vectors.status(connection).ready:
        return "unavailable"
    claimed = _claim(document_id, model_id, generation, force=force)
    if isinstance(claimed, str):
        log.info("documents.embedding.skipped", reason=claimed)
        return claimed
    document, model = claimed.document, claimed.model
    bind_request_context(
        document_id=str(document_id),
        embedding_generation=generation,
        organization_id=str(document.organization_id),
    )
    chunks = list(
        DocumentChunk.objects.filter(document_id=document_id)
        .order_by("chunk_index")
        .values_list("id", "content", "content_sha256")
    )
    current = {chunk_id: sha for chunk_id, _, sha in chunks}
    stored = vectors.existing(connection, document_id, model.id)
    needed = [c for c in chunks if force or stored.get(c[0]) != c[2]]
    reused = len(chunks) - len(needed)
    if not chunks:
        return _fail(claimed, "no_chunks", "The document has no passages to embed.", started)

    estimate = sum(estimate_text_tokens(content) for _, content, _ in needed)
    if needed and _tokens_today(document.organization_id) + estimate > (
        settings.NEXA_EMBEDDING_ORG_TOKENS_PER_DAY
    ):
        return _fail(
            claimed,
            "quota_exceeded",
            "The organization's daily embedding budget is used up; it resets tomorrow.",
            started,
            retryable=False,
        )

    spec_ = spec(model)
    batch = max(1, min(settings.NEXA_EMBEDDING_BATCH_SIZE, model.max_batch_size))
    embedded = 0
    tokens = 0
    try:
        for offset in range(0, len(needed), batch):
            part = needed[offset : offset + batch]
            result = _embed([content for _, content, _ in part], spec_)
            tokens += result.input_tokens
            _record_usage(document, model, result, status="success")
            outcome = _store_batch(claimed, generation, part, result.vectors)
            if outcome != "ok":
                log.info("documents.embedding.stopped", reason=outcome)
                return outcome
            embedded += len(part)
    except AIError as exc:
        _record_usage(document, model, None, status="error", error=exc)
        retryable = exc.retryable or isinstance(exc, AITimeout)
        log.warning("documents.embedding.provider_failed", embedded=embedded, **exc.log_fields())
        outcome = _fail(
            claimed,
            "provider_unavailable" if retryable else "provider_error",
            "The embedding provider could not be reached; it will be tried again."
            if retryable
            else "The embedding provider refused the request.",
            started,
            retryable=retryable,
        )
        return "retry" if retryable and outcome == "failed" else outcome
    except SoftTimeLimitExceeded:
        return _fail(claimed, "timeout", "Embedding took too long.", started, retryable=True)
    except Exception as exc:
        log.error("documents.embedding.crashed", error=type(exc).__name__)
        return _fail(claimed, "embedding_failed", "The document could not be embedded.", started)
    return _finalize(claimed, generation, current, embedded, reused, tokens, started)


def _claim(document_id: UUID, model_id: UUID, generation: int, *, force: bool) -> _Claim | str:
    now = timezone.now()
    with transaction.atomic():
        # The lock order of every document writer: document, then its index, then this state.
        document = Document.objects.select_for_update().filter(id=document_id).first()
        index = DocumentIndex.objects.select_for_update().filter(document_id=document_id).first()
        state = (
            DocumentEmbedding.objects.select_for_update()
            .select_related("embedding_model")
            .filter(document_id=document_id, embedding_model_id=model_id)
            .first()
        )
        if document is None or state is None or document.deleted_at is not None:
            return "cancelled"
        if state.generation != generation:
            return "superseded"
        if index is None or index.indexed_generation is None or not index.searchable:
            return "skipped"  # nothing indexed to embed; indexing requests embedding again
        if document.status != lifecycle.COMPLETED:
            return "skipped"
        if (
            not force
            and state.status == DocumentEmbedding.Status.READY
            and state.source_generation == index.indexed_generation
        ):
            return "skipped"
        if (
            state.status == DocumentEmbedding.Status.EMBEDDING
            and state.started_at
            and (state.started_at > now - 2 * timedelta(seconds=job_timeout()))
        ):
            return "skipped"  # a live run
        state.status = DocumentEmbedding.Status.EMBEDDING
        state.started_at = now
        state.finished_at = None
        state.attempt_count += 1
        state.save()
        return _Claim(document, index, state, state.embedding_model)


def _embed(texts: Sequence[str], model: EmbeddingModel) -> Embeddings:
    async def run() -> Embeddings:
        embedder = Embedder(
            build_embedding_providers(
                openai_api_key=settings.NEXA_AI_OPENAI_API_KEY,
                openai_base_url=settings.NEXA_AI_OPENAI_BASE_URL,
                mock_enabled=settings.NEXA_EMBEDDING_MOCK_ALLOWED,
            ),
            timeout=settings.NEXA_EMBEDDING_TIMEOUT_SECONDS,
            max_attempts=settings.NEXA_EMBEDDING_MAX_ATTEMPTS,
            max_batch_tokens=settings.NEXA_EMBEDDING_MAX_BATCH_TOKENS,
        )
        try:
            return await embedder.embed_documents(texts, model)
        finally:
            await embedder.aclose()

    return asyncio.run(run())


def _store_batch(
    claimed: _Claim,
    generation: int,
    part: Sequence[tuple[UUID, str, str]],
    batch_vectors: Sequence[Sequence[float]],
) -> str:
    """Store one batch, unless the run has been superseded or the document deleted."""
    document_id, model = claimed.document.id, claimed.model
    with transaction.atomic():
        document = Document.objects.select_for_update().filter(id=document_id).first()
        state = (
            DocumentEmbedding.objects.select_for_update()
            .filter(document_id=document_id, embedding_model=model)
            .first()
        )
        if document is None or state is None or document.deleted_at is not None:
            return "cancelled"
        if state.generation != generation:
            return "superseded"
        vectors.upsert(
            connection,
            organization_id=document.organization_id,
            document_id=document_id,
            model_id=model.id,
            space=model.space,
            dimensions=model.dimensions,
            items=[
                (chunk_id, sha, vector)
                for (chunk_id, _, sha), vector in zip(part, batch_vectors, strict=True)
            ],
        )
    return "ok"


def _finalize(
    claimed: _Claim,
    generation: int,
    current: dict[UUID, str],
    embedded: int,
    reused: int,
    tokens: int,
    started: float,
) -> str:
    document_id, model = claimed.document.id, claimed.model
    now = timezone.now()
    with transaction.atomic():
        document = Document.objects.select_for_update().filter(id=document_id).first()
        index = DocumentIndex.objects.select_for_update().filter(document_id=document_id).first()
        state = (
            DocumentEmbedding.objects.select_for_update()
            .filter(document_id=document_id, embedding_model=model)
            .first()
        )
        if document is None or state is None or index is None or document.deleted_at is not None:
            return "cancelled"
        if state.generation != generation:
            return "superseded"
        vectors.prune(connection, document_id, model.id, list(current))
        complete = vectors.count_current(connection, document_id, model.id, current)
        state.chunk_count = len(current)
        state.embedded_count = embedded
        state.reused_count = reused
        state.input_tokens += tokens
        state.duration_ms = _elapsed_ms(started)
        state.finished_at = now
        if complete != len(current) or index.indexed_generation is None:
            state.status = DocumentEmbedding.Status.FAILED
            state.error_code = "incomplete"
            state.error_message = "Some passages have no vector; embedding will be retried."
            state.retryable = True
            state.save()
            return "retry"
        state.status = DocumentEmbedding.Status.READY
        state.source_generation = index.indexed_generation
        state.space = model.space
        state.ready_at = now
        state.error_code = ""
        state.error_message = ""
        state.retryable = False
        state.save()
    log.info(
        "documents.embedding.completed",
        chunks=len(current),
        embedded=embedded,
        reused=reused,
        input_tokens=tokens,
        duration_ms=state.duration_ms,
        model=model.key,
    )
    return "ready"


def _fail(
    claimed: _Claim,
    code: str,
    message: str,
    started: float,
    *,
    retryable: bool = False,
) -> str:
    with transaction.atomic():
        state = (
            DocumentEmbedding.objects.select_for_update()
            .filter(pk=claimed.state.pk, generation=claimed.state.generation)
            .first()
        )
        if state is None:
            return "superseded"
        state.status = DocumentEmbedding.Status.FAILED
        state.error_code = code
        state.error_message = message[:MAX_ERROR_MESSAGE_CHARS]
        state.retryable = retryable
        state.finished_at = timezone.now()
        state.duration_ms = _elapsed_ms(started)
        state.save()
    log.warning("documents.embedding.failed", error_code=code, retryable=retryable)
    return "failed"


def fail_stalled(now: datetime) -> int:
    """Runs whose worker died: failed (retryable), so a re-index or the next request redoes
    them."""
    cutoff = now - 2 * timedelta(seconds=job_timeout())
    return DocumentEmbedding.objects.filter(
        status=DocumentEmbedding.Status.EMBEDDING, started_at__lt=cutoff
    ).update(
        status=DocumentEmbedding.Status.FAILED,
        error_code="timeout",
        error_message="Embedding did not finish; it can be requested again.",
        retryable=True,
        finished_at=now,
    )


def job_timeout() -> int:
    return int(settings.NEXA_EMBEDDING_JOB_TIMEOUT_SECONDS)


def _tokens_today(organization_id: UUID) -> int:
    since = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = AIUsageEvent.objects.filter(
        organization_id=organization_id, feature="embedding", created_at__gte=since
    ).aggregate(total=Sum("total_tokens"))["total"]
    return int(total or 0)


def _record_usage(
    document: Document,
    model: AIEmbeddingModel,
    result: Embeddings | None,
    *,
    status: str,
    error: AIError | None = None,
) -> None:
    """One ledger row per provider batch (metadata only, never text)."""
    tokens = result.input_tokens if result is not None else 0
    AIUsageEvent.objects.create(
        id=new_id(),
        organization_id=document.organization_id,
        user_id=document.uploaded_by_id or NIL_USER,
        actor_type="system",
        source_type="document",
        source_id=document.id,
        feature="embedding",
        alias=model.key,
        provider=model.provider,
        provider_model=model.provider_model,
        input_tokens=tokens,
        output_tokens=0,
        total_tokens=tokens,
        cost_micros=result.cost_micros if result is not None else None,
        latency_ms=result.latency_ms if result is not None else 0,
        status=status,
        error_code=error.error_code.value if error is not None else "",
        attempts=result.requests if result is not None else 1,
        usage_source=result.usage_source if result is not None else "provider",
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


# --- Reporting -----------------------------------------------------------------------------------


def semantic_payload(document: Document, *, state: Any = None) -> dict[str, Any]:
    """The semantic index state of a document as the API shows it, for the active model.
    `status`: unavailable (with `reason`), none, pending, embedding, ready, failed, stale
    (vectors for older chunks: the current ones are being embedded)."""
    status = availability()
    model = status.model
    if not status.enabled or model is None:
        return {"status": "unavailable", "reason": status.reason, "model": None, "ready": False}
    if state is None:
        # Lists prefetch `embedding_states`; a single document reads its own.
        cache = getattr(document, "_prefetched_objects_cache", {})
        states = (
            cache["embedding_states"]
            if "embedding_states" in cache
            else DocumentEmbedding.objects.filter(document=document)
        )
        state = next((s for s in states if s.embedding_model_id == model.id), None)
    if state is None:
        return {"status": "none", "reason": None, "model": model.key, "ready": False}
    index = getattr(document, "search_index", None)
    indexed = index.indexed_generation if isinstance(index, DocumentIndex) else None
    ready = state.status == DocumentEmbedding.Status.READY and state.source_generation == indexed
    shown = state.status
    if state.status == DocumentEmbedding.Status.READY and not ready:
        shown = "stale"
    return {
        "status": shown,
        "reason": None,
        "model": model.key,
        "ready": ready,
        "chunk_count": state.chunk_count,
        "error_code": state.error_code or None,
        "error_message": state.error_message or None,
        "retryable": state.retryable,
        "ready_at": state.ready_at.isoformat() if state.ready_at else None,
    }
