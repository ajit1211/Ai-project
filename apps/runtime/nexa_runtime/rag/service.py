"""The RAG engine: deciding what a grounded question may search, finding the evidence, and
keeping the record of what was cited (docs/architecture/rag.md).

    send(grounding) ─► resolve: authorize the scope now, before anything is stored
    reply           ─► retrieve: authorize again inside the search query, rank, drop repeats,
                       fit the evidence budget
                    ─► the chat engine calls the model with the evidence (evidence.py)
    reply stored    ─► store_sources: every passage given, and whether the reply cited it
    messages read   ─► sources_for: passages re-read and documents re-authorized every time

Explicit document selections go through the shared policy (`nexa_common.access.can`) one by
one; every search also applies the policy's SQL form (retrieval.visible_documents), so a
document unshared between the two steps is not searched either.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, insert, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nexa_ai.embeddings import Embedder, EmbeddingModel, Embeddings
from nexa_ai.errors import AIError
from nexa_ai.gateway import UsageRecord, UsageSink
from nexa_ai.types import Feature
from nexa_common.access import Principal, ResourceRef, Visibility, can, require
from nexa_common.errors import FieldError, NotFound, ValidationFailed
from nexa_common.ids import new_id
from nexa_common.logs import get_logger
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.rag import tables as t
from nexa_runtime.rag.errors import (
    DocumentNotIndexed,
    DocumentUnavailable,
    GroundingDisabled,
    InvalidScope,
    RetrievalFailed,
)
from nexa_runtime.rag.evidence import CitationCheck, Evidence, build_evidence
from nexa_runtime.rag.fusion import (
    NoReranker,
    Reranker,
    reciprocal_rank_fusion,
    rerank_safely,
)
from nexa_runtime.rag.retrieval import (
    DocumentFilter,
    LexicalRetriever,
    Passage,
    RetrievalQuery,
    Retriever,
    count_searchable,
    drop_near_duplicates,
    query_terms,
)
from nexa_runtime.rag.schemas import (
    MAX_SOURCES,
    GroundingRequest,
    GroundingScopeKind,
    SourceOut,
)
from nexa_runtime.rag.vector import SemanticCatalog, VectorRetriever, count_semantic_ready
from nexa_runtime.ratelimit import Limiter

log = get_logger("nexa.runtime.rag")


@dataclass(frozen=True, slots=True)
class Scope:
    """A grounding scope that has been authorized for one principal."""

    kind: GroundingScopeKind
    document_ids: tuple[UUID, ...] = ()
    team_id: UUID | None = None
    max_sources: int = 6
    # Phase 09: "auto" (the deployment's default), "lexical", "semantic" or "hybrid".
    retrieval_mode: str = "auto"

    def record(self) -> dict[str, Any]:
        """What is stored on the reply when it is created (`messages.grounding`)."""
        return {
            "mode": "documents",
            "scope": self.kind,
            "document_ids": [str(i) for i in self.document_ids],
            "team_id": str(self.team_id) if self.team_id else None,
            "max_sources": self.max_sources,
            "retrieval_requested": self.retrieval_mode,
            "outcome": "pending",
        }

    @staticmethod
    def request_from_record(record: Any) -> GroundingRequest | None:
        """The request a stored reply was made with, to authorize again when regenerating."""
        if not isinstance(record, dict) or record.get("mode") != "documents":
            return None
        kind = record.get("scope")
        return GroundingRequest(
            document_ids=[UUID(i) for i in record.get("document_ids") or []]
            if kind == "documents"
            else [],
            team_id=UUID(record["team_id"]) if kind == "team" and record.get("team_id") else None,
            all_documents=kind == "library",
            max_sources=_clamp_sources(record.get("max_sources")),
            retrieval_mode=record.get("retrieval_requested")
            if record.get("retrieval_requested") in RETRIEVAL_MODES
            else "auto",
        )


@dataclass(frozen=True, slots=True)
class Grounded:
    """The evidence found for one reply."""

    scope: Scope
    evidence: Evidence
    terms: int
    retrieved: int
    searched_documents: int
    retrieval_ms: int
    strategy: str
    # Phase 09: the retrieval that actually ran, and why it differs from the request, if it does.
    retrieval_mode: str = "lexical"
    fallback: str | None = None

    def record(self, status: str, check: CitationCheck | None) -> dict[str, Any]:
        """The reply's grounding record once it has finished."""
        supplied = len(self.evidence.passages)
        if status != "completed":
            outcome = status  # "failed" or "cancelled"
        elif supplied == 0:
            outcome = "no_evidence"
        elif check is not None and check.valid_count:
            outcome = "answered"
        else:
            outcome = "uncited"
        return {
            **self.scope.record(),
            "outcome": outcome,
            "searched_documents": self.searched_documents,
            "retrieved": self.retrieved,
            "supplied": supplied,
            "cited": check.valid_count if check else 0,
            "invalid_citations": list(check.invalid) if check else [],
            "retrieval_ms": self.retrieval_ms,
            "strategy": self.strategy,
            "retrieval_mode": self.retrieval_mode,
            "fallback": self.fallback,
        }


def failed_record(scope: Scope, status: str) -> dict[str, Any]:
    """The record of a reply that ended before any evidence was found."""
    return {**scope.record(), "outcome": status}


class RagEngine:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        settings: RuntimeSettings,
        limiter: Limiter | None = None,
        retriever: Retriever | None = None,
        embedder: Embedder | None = None,
        usage: UsageSink | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._limiter = limiter or Limiter(None)
        self._retriever: Retriever = retriever or LexicalRetriever()
        self._embedder = embedder or Embedder({})
        self.semantic = SemanticCatalog(engine, settings, self._embedder)
        self._vector = VectorRetriever(ef_search=settings.rag_hnsw_ef_search)
        self._usage = usage
        self._reranker: Reranker = reranker or NoReranker()

    @property
    def strategy(self) -> str:
        return self._retriever.name

    # --- Authorizing a scope -------------------------------------------------------------------

    async def resolve(
        self,
        principal: Principal,
        request: GroundingRequest,
        *,
        question: str,
        conn: AsyncConnection | None = None,
    ) -> Scope:
        """Check the requested scope for this principal, or raise: 404 for a document (or team)
        that does not exist for them, 409 for one they may read that is not indexed yet."""
        settings = self._settings
        if not settings.rag_enabled:
            raise GroundingDisabled("Answers from documents are switched off.")
        require(principal, "documents.view")
        if len(question) > settings.rag_max_question_chars:
            raise ValidationFailed(
                details=[
                    FieldError(
                        field="content",
                        code="too_long",
                        message=(
                            "A question about documents can be at most "
                            f"{settings.rag_max_question_chars} characters."
                        ),
                    )
                ]
            )
        chosen = [bool(request.document_ids), request.team_id is not None, request.all_documents]
        if sum(chosen) != 1:
            raise InvalidScope("Choose documents, a team, or all documents to answer from.")
        await self._limiter.hit(
            "rag_queries",
            str(principal.user_id),
            limit=settings.rag_queries_per_minute,
            window_seconds=60,
        )
        max_sources = max(
            1, min(request.max_sources or settings.rag_top_k, settings.rag_max_top_k, MAX_SOURCES)
        )
        organization_id = _organization(principal)
        mode = request.retrieval_mode

        if request.document_ids:
            ids = tuple(dict.fromkeys(request.document_ids))
            if len(ids) > settings.rag_max_documents:
                raise InvalidScope(f"Select at most {settings.rag_max_documents} documents.")
            rows = await self._read(conn, _documents, organization_id, ids)
            not_ready: list[UUID] = []
            for document_id in ids:
                row = rows.get(document_id)
                if row is None or row.deleted_at is not None:
                    raise DocumentUnavailable
                if not can(principal, "view", _resource(row)):
                    raise DocumentUnavailable
                if row.status != "completed" or row.indexed_at is None or not row.chunk_count:
                    not_ready.append(document_id)
            if not_ready:
                raise DocumentNotIndexed(
                    details=[
                        FieldError(
                            field="grounding.document_ids",
                            code="not_indexed",
                            message=f"Document {document_id} is not ready to be searched yet.",
                        )
                        for document_id in not_ready
                    ]
                )
            return Scope(
                "documents", document_ids=ids, max_sources=max_sources, retrieval_mode=mode
            )

        if request.team_id is not None:
            allowed = request.team_id in principal.team_ids or principal.has("documents.manage_all")
            if allowed:
                allowed = await self._read(conn, _team_exists, organization_id, request.team_id)
            if not allowed:
                raise DocumentUnavailable("The selected team is not available.")
            return Scope(
                "team", team_id=request.team_id, max_sources=max_sources, retrieval_mode=mode
            )

        return Scope("library", max_sources=max_sources, retrieval_mode=mode)

    async def _read(self, conn: AsyncConnection | None, query: Any, *args: Any) -> Any:
        """Run a lookup on the caller's connection when it has one (a caller holding a
        conversation lock must not wait for a second pooled connection)."""
        if conn is not None:
            return await query(conn, *args)
        async with self._engine.connect() as own:
            return await query(own, *args)

    def filter_for(self, principal: Principal, scope: Scope) -> DocumentFilter:
        return DocumentFilter(
            organization_id=_organization(principal),
            user_id=principal.user_id,
            team_ids=principal.team_ids,
            manage_all=principal.has("documents.manage_all"),
            document_ids=scope.document_ids if scope.kind == "documents" else None,
            team_id=scope.team_id if scope.kind == "team" else None,
        )

    # --- Finding evidence ------------------------------------------------------------------------

    async def retrieve(
        self,
        principal: Principal,
        scope: Scope,
        *,
        question: str,
        request_id: str | None = None,
    ) -> Grounded:
        """Find the evidence for one question with the requested (or default) retrieval:
        lexical, semantic, or both fused by RRF. A semantic side that cannot run falls back to
        lexical retrieval, and the record says so (`fallback`)."""
        settings = self._settings
        terms = query_terms(question)
        documents = self.filter_for(principal, scope)
        started = time.perf_counter()
        semantic = await self.semantic.status()
        lexical_on, semantic_on, fallback = _methods(
            scope.retrieval_mode, settings.rag_retrieval_mode, semantic.available, semantic.reason
        )
        vector: list[float] | None = None
        embed_ms: int | None = None
        if semantic_on and semantic.model is not None:
            vector, embed_ms = await self._embed_question(
                principal, question, semantic.model, request_id
            )
            if vector is None:
                semantic_on, lexical_on, fallback = False, True, "query_embedding_failed"
        timeout_ms = int(settings.rag_retrieval_timeout_ms)
        lexical: list[Passage] = []
        similar: list[Passage] = []
        semantic_documents = 0
        vector_failed = False
        searched = 0
        try:
            async with asyncio.timeout(timeout_ms / 1000 + 1):
                async with self._engine.connect() as conn, conn.begin():
                    # A runaway query is stopped by the database, not only abandoned here.
                    await conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                    searched = await count_searchable(conn, documents)
                    query = RetrievalQuery(
                        text=question,
                        terms=terms,
                        scope=documents,
                        limit=max(settings.rag_candidates, scope.max_sources),
                        min_matched_terms=min(settings.rag_min_matched_terms, max(1, len(terms))),
                    )
                    if lexical_on and terms and searched:
                        lexical = await self._retriever.search(conn, query)
                    if (
                        semantic_on
                        and vector is not None
                        and semantic.model is not None
                        and searched
                    ):
                        try:
                            # A savepoint: a failed vector query leaves the lexical results usable.
                            async with conn.begin_nested():
                                semantic_query = replace(
                                    query,
                                    vector=vector,
                                    model=semantic.model,
                                    limit=max(settings.rag_vector_candidates, scope.max_sources),
                                )
                                semantic_documents = await count_semantic_ready(
                                    conn, semantic_query, semantic.model
                                )
                                similar = await self._vector.search(conn, semantic_query)
                        except SQLAlchemyError as exc:
                            log.error("rag.vector_search.failed", error=type(exc).__name__)
                            vector_failed, semantic_on = True, False
                            fallback = "vector_search_failed"
                            if not lexical_on and terms:
                                lexical_on = True
                                lexical = await self._retriever.search(conn, query)
        except (TimeoutError, SQLAlchemyError) as exc:
            log.error("rag.retrieval.failed", scope=scope.kind, error=type(exc).__name__)
            raise RetrievalFailed from exc
        if lexical_on and semantic_on:
            mode = "hybrid"
            candidates = reciprocal_rank_fusion(
                {"lexical": lexical, "semantic": similar}, k=settings.rag_rrf_k
            )
            strategy = f"rrf(k={settings.rag_rrf_k}):{self._retriever.name}+{self._vector.name}"
        elif semantic_on:
            mode, candidates, strategy = "semantic", similar, self._vector.name
        else:
            mode, candidates, strategy = "lexical", lexical, self._retriever.name
        distinct = drop_near_duplicates(candidates)
        ordered, reranked = await rerank_safely(
            self._reranker,
            question,
            distinct[: max(settings.rag_candidates, scope.max_sources)],
            timeout_seconds=settings.rag_query_embedding_timeout_seconds,
        )
        evidence = build_evidence(
            ordered[: scope.max_sources],
            budget_tokens=settings.rag_context_max_tokens,
            max_passage_chars=settings.rag_passage_max_chars,
        )
        retrieval_ms = max(0, int((time.perf_counter() - started) * 1000))
        log.info(
            "rag.retrieval.completed",
            scope=scope.kind,
            requested=scope.retrieval_mode,
            mode=mode,
            fallback=fallback,
            terms=len(terms),
            searched_documents=searched,
            semantic_documents=semantic_documents,
            lexical_candidates=len(lexical),
            semantic_candidates=len(similar),
            vector_failed=vector_failed,
            query_embedding_ms=embed_ms,
            embedding_model=semantic.model.key if mode != "lexical" and semantic.model else None,
            near_duplicates=len(candidates) - len(distinct),
            reranked=reranked,
            supplied=len(evidence.passages),
            dropped_for_budget=evidence.dropped,
            evidence_tokens=evidence.estimated_tokens,
            retrieval_ms=retrieval_ms,
            strategy=strategy,
        )
        return Grounded(
            scope=scope,
            evidence=evidence,
            terms=len(terms),
            retrieved=len(distinct),
            searched_documents=searched,
            retrieval_ms=retrieval_ms,
            strategy=strategy,
            retrieval_mode=mode,
            fallback=fallback,
        )

    async def _embed_question(
        self,
        principal: Principal,
        question: str,
        model: EmbeddingModel,
        request_id: str | None,
    ) -> tuple[list[float] | None, int]:
        """The question's vector, or None when it cannot be made now (the caller falls back to
        lexical retrieval). Only the question is sent to the provider, never document text."""
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.rag_query_embedding_timeout_seconds + 1):
                result = await self._embedder.embed_query(question, model)
        except (AIError, TimeoutError) as exc:
            elapsed = _ms(started)
            code = exc.error_code.value if isinstance(exc, AIError) else "AI_TIMEOUT"
            log.warning("rag.query_embedding.failed", error_code=code, model=model.key)
            await self._record_usage(principal, model, None, request_id, elapsed, error_code=code)
            return None, elapsed
        elapsed = _ms(started)
        await self._record_usage(principal, model, result, request_id, elapsed)
        return result.vectors[0], elapsed

    async def _record_usage(
        self,
        principal: Principal,
        model: EmbeddingModel,
        result: Embeddings | None,
        request_id: str | None,
        latency_ms: int,
        *,
        error_code: str | None = None,
    ) -> None:
        """One usage-ledger row per question embedding (metadata only, never the question)."""
        if self._usage is None or principal.organization_id is None:
            return
        tokens = result.input_tokens if result is not None else 0
        record = UsageRecord(
            id=new_id(),
            created_at=datetime.now(UTC),
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            actor_type=principal.actor_type,
            request_id=request_id,
            source_type="rag_query",
            source_id=None,
            feature=Feature.EMBEDDING.value,
            alias=model.key,
            provider=model.provider,
            provider_model=model.provider_model,
            model_id=None,
            price_id=None,
            input_tokens=tokens,
            output_tokens=0,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            total_tokens=tokens,
            cost_micros=result.cost_micros if result is not None else None,
            latency_ms=latency_ms,
            ttft_ms=None,
            status="success" if result is not None else "error",
            error_code=error_code,
            attempts=result.requests if result is not None else 1,
            fallback_used=False,
            stream=False,
            structured=False,
            usage_source=result.usage_source if result is not None else "estimated",
        )
        try:
            await self._usage.record(record)
        except Exception as exc:
            log.error("rag.query_embedding.usage_failed", error=type(exc).__name__)

    # --- Sources ----------------------------------------------------------------------------------

    async def store_sources(
        self,
        conn: AsyncConnection,
        *,
        organization_id: UUID,
        message_id: UUID,
        grounded: Grounded,
        check: CitationCheck | None,
    ) -> None:
        evidence = grounded.evidence
        if not evidence.passages:
            return
        cited = set(check.cited) if check else set()
        created = datetime.now(UTC)
        await conn.execute(
            insert(t.message_sources),
            [
                {
                    "id": new_id(),
                    "organization_id": organization_id,
                    "message_id": message_id,
                    "document_id": passage.document_id,
                    "chunk_id": passage.chunk_id,
                    "label": label,
                    "rank": rank,
                    "score": passage.score,
                    "cited": label in cited,
                    "created_at": created,
                }
                for rank, (label, passage) in enumerate(
                    zip(evidence.labels, evidence.passages, strict=True), start=1
                )
            ],
        )

    def sources_from(self, grounded: Grounded, check: CitationCheck | None) -> list[SourceOut]:
        """The sources of a reply being generated, from the passages just retrieved (which
        were authorized by the search itself)."""
        cited = set(check.cited) if check else set()
        evidence = grounded.evidence
        return [
            _source_out(
                label,
                rank,
                cited=label in cited,
                score=passage.score,
                passage=passage,
                excerpt_chars=self._settings.rag_excerpt_chars,
            )
            for rank, (label, passage) in enumerate(
                zip(evidence.labels, evidence.passages, strict=True), start=1
            )
        ]

    async def sources_for(
        self,
        conn: AsyncConnection,
        principal: Principal,
        message_ids: Sequence[UUID],
        *,
        detail: bool = False,
    ) -> dict[UUID, list[SourceOut]]:
        """The stored sources of these (already authorized) messages, re-read now: a passage
        whose document was deleted, unshared or re-indexed differently shows as unavailable."""
        if not message_ids:
            return {}
        organization_id = _organization(principal)
        ms, c = t.message_sources.c, t.document_chunks.c
        rows = (
            await conn.execute(
                select(t.message_sources)
                .where(ms.organization_id == organization_id, ms.message_id.in_(list(message_ids)))
                .order_by(ms.message_id, ms.rank)
            )
        ).all()
        document_ids = tuple({row.document_id for row in rows if row.document_id is not None})
        documents = await _documents(conn, organization_id, document_ids)
        readable = {
            document_id
            for document_id, row in documents.items()
            if row.deleted_at is None and can(principal, "view", _resource(row))
        }
        chunk_ids = [row.chunk_id for row in rows if row.document_id in readable]
        chunks = {}
        if chunk_ids:
            chunks = {
                chunk.id: chunk
                for chunk in (
                    await conn.execute(
                        select(t.document_chunks).where(
                            c.organization_id == organization_id, c.id.in_(chunk_ids)
                        )
                    )
                ).all()
            }
        limit = (
            self._settings.rag_source_detail_chars if detail else self._settings.rag_excerpt_chars
        )
        found: dict[UUID, list[SourceOut]] = defaultdict(list)
        for row in rows:
            chunk = chunks.get(row.chunk_id) if row.document_id in readable else None
            if chunk is None or chunk.document_id != row.document_id:
                found[row.message_id].append(
                    SourceOut(label=row.label, rank=row.rank, cited=row.cited, available=False)
                )
                continue
            document = documents[row.document_id]
            passage = Passage(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                kind=chunk.kind,
                content=chunk.content,
                section=chunk.section,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                row_start=chunk.row_start,
                row_end=chunk.row_end,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_estimate=chunk.token_estimate,
                document_title=document.title or document.filename,
                filename=document.filename,
                format=document.format,
                score=row.score,
                matched_terms=0,
            )
            found[row.message_id].append(
                _source_out(
                    row.label,
                    row.rank,
                    cited=row.cited,
                    score=row.score,
                    passage=passage,
                    excerpt_chars=limit,
                )
            )
        return dict(found)


def _source_out(
    label: str, rank: int, *, cited: bool, score: float, passage: Passage, excerpt_chars: int
) -> SourceOut:
    content = passage.content
    truncated = len(content) > excerpt_chars
    return SourceOut(
        label=label,
        rank=rank,
        cited=cited,
        available=True,
        score=round(score, 4),
        document_id=passage.document_id,
        chunk_id=passage.chunk_id,
        document_title=passage.document_title,
        filename=passage.filename,
        format=passage.format,
        section=passage.section or None,
        page_start=passage.page_start,
        page_end=passage.page_end,
        row_start=passage.row_start,
        row_end=passage.row_end,
        char_start=passage.char_start,
        char_end=passage.char_end,
        excerpt=content[:excerpt_chars].rstrip() + ("…" if truncated else ""),
        excerpt_truncated=truncated,
    )


def _resource(row: Any) -> ResourceRef:
    return ResourceRef(
        type="documents",
        id=row.id,
        organization_id=row.organization_id,
        owner_id=row.uploaded_by_id,
        visibility=cast(Visibility, row.visibility),
        team_id=row.team_id,
    )


async def _documents(
    conn: AsyncConnection, organization_id: UUID, document_ids: Sequence[UUID]
) -> dict[UUID, Any]:
    """Documents of one organization with their index state, by id."""
    if not document_ids:
        return {}
    d, i = t.documents.c, t.document_indexes.c
    rows = (
        await conn.execute(
            select(
                d.id,
                d.organization_id,
                d.uploaded_by_id,
                d.team_id,
                d.visibility,
                d.filename,
                d.title,
                d.format,
                d.status,
                d.deleted_at,
                i.indexed_at,
                i.chunk_count,
            )
            .select_from(
                t.documents.outerjoin(
                    t.document_indexes,
                    and_(i.document_id == d.id, i.organization_id == d.organization_id),
                )
            )
            .where(d.organization_id == organization_id, d.id.in_(list(document_ids)))
        )
    ).all()
    return {row.id: row for row in rows}


async def _team_exists(conn: AsyncConnection, organization_id: UUID, team_id: UUID) -> bool:
    result = await conn.execute(
        select(t.teams.c.id).where(
            t.teams.c.id == team_id, t.teams.c.organization_id == organization_id
        )
    )
    return result.first() is not None


def _clamp_sources(value: Any) -> int | None:
    """Within 1..MAX_SOURCES whatever the settings say, so a stored scope always
    validates again when the reply is regenerated."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(1, min(value, MAX_SOURCES))


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id


RETRIEVAL_MODES = ("auto", "lexical", "semantic", "hybrid")


def _methods(
    requested: str, default: str, semantic_available: bool, semantic_reason: str
) -> tuple[bool, bool, str | None]:
    """(lexical, semantic, fallback) for a requested mode. "auto" follows the deployment's
    default and quietly uses lexical retrieval where semantic search does not run here; an
    explicit request for semantic or hybrid retrieval that cannot run is recorded as a
    fallback (`semantic_<reason>`)."""
    mode = default if requested == "auto" else requested
    if mode == "lexical":
        return True, False, None
    if not semantic_available:
        fallback = None if requested == "auto" else f"semantic_{semantic_reason}"
        return True, False, fallback
    if mode == "semantic":
        return False, True, None
    return True, True, None


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
