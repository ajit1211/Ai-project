"""Semantic retrieval: the question's vector compared with the vectors of authorized chunks
(docs/architecture/vector-search.md#retrieval).

    SemanticCatalog.status()  ─► is the vector store there, which embedding model is active
                                 (the rule Core uses too), can this process embed with it
    VectorRetriever.search()  ─► cosine distance (pgvector `<=>`) between the question's vector
                                 and the stored vectors of the active model only, for chunks of
                                 documents that are visible, indexed, and semantically ready
                                 for their current chunks; best first

Authorization is in the query, as for lexical retrieval: the same `visible_documents`
predicate, the caller's organization, and only vectors whose text hash matches the chunk's
current text. Results carry a cosine similarity (1 - distance): it orders passages for one
question and is compared with the model's heuristic `relevance_floor`; it is not a confidence.
Without an approximate index the search is exact; with one (manage.py vector_index create) it
is approximate, and `RAG_HNSW_EF_SEARCH` bounds the candidates it considers.
"""

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Float, Text, and_, cast, literal, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nexa_ai.embeddings import Embedder, EmbeddingModel, choose_model
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.rag import tables as t
from nexa_runtime.rag.retrieval import Passage, RetrievalQuery, _searchable_join, visible_documents


@dataclass(frozen=True, slots=True)
class SemanticStatus:
    reason: str  # ok | disabled | pgvector_missing | no_model | provider_not_configured
    model: EmbeddingModel | None = None

    @property
    def available(self) -> bool:
        return self.reason == "ok" and self.model is not None


class SemanticCatalog:
    """Whether semantic search can run, cached for `rag_semantic_status_ttl_seconds`."""

    def __init__(self, engine: AsyncEngine, settings: RuntimeSettings, embedder: Embedder) -> None:
        self._engine = engine
        self._settings = settings
        self._embedder = embedder
        self._cached: tuple[float, SemanticStatus] | None = None

    def invalidate(self) -> None:
        self._cached = None

    async def status(self) -> SemanticStatus:
        now = time.monotonic()
        if (
            self._cached is not None
            and now - self._cached[0] < self._settings.rag_semantic_status_ttl_seconds
        ):
            return self._cached[1]
        result = await self._load()
        self._cached = (now, result)
        return result

    async def _load(self) -> SemanticStatus:
        if not self._settings.embedding_enabled:
            return SemanticStatus("disabled")
        async with self._engine.connect() as conn:
            ready = (
                await conn.execute(
                    text(
                        "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector') "
                        "AND to_regclass('public.chunk_embeddings') IS NOT NULL"
                    )
                )
            ).scalar_one()
            if not ready:
                return SemanticStatus("pgvector_missing")
            m = t.ai_embedding_models.c
            rows = (
                await conn.execute(select(t.ai_embedding_models).where(m.is_active.is_(True)))
            ).all()
        models = [model_from_row(row) for row in rows]
        chosen = choose_model(
            models,
            configured=self._settings.embedding_model,
            available_providers=set(self._embedder.providers),
            mock_allowed=self._settings.mock_provider_enabled,
        )
        if chosen is None:
            return SemanticStatus("no_model")
        if not self._embedder.supports(chosen):
            return SemanticStatus("provider_not_configured", chosen)
        return SemanticStatus("ok", chosen)


def model_from_row(row: Any) -> EmbeddingModel:
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


def vector_literal(vector: Any) -> str:
    return "[" + ",".join(f"{float(value):.7g}" for value in vector) + "]"


class VectorRetriever:
    """pgvector cosine search (Phase 09)."""

    name = "pgvector-cosine/v1"

    def __init__(self, *, ef_search: int = 100) -> None:
        self._ef_search = max(1, int(ef_search))

    async def search(self, conn: AsyncConnection, query: RetrievalQuery) -> list[Passage]:
        model: EmbeddingModel | None = query.model
        if query.vector is None or model is None or model.id is None or query.limit <= 0:
            return []
        dims = int(model.dimensions)
        # Inlined (a validated UUID) so a partial HNSW index on this model can match the plan.
        model_id: Any = literal_column(f"'{UUID(str(model.id))}'::uuid")
        c, d, i = t.document_chunks.c, t.documents.c, t.document_indexes.c
        s, e = t.document_embeddings.c, t.chunk_embeddings.c
        question = cast(cast(literal(vector_literal(query.vector)), Text), t.Vector(dims))
        distance = cast(e.embedding, t.Vector(dims)).op("<=>", return_type=Float)(question)
        stmt = (
            select(
                c.id,
                c.document_id,
                c.chunk_index,
                c.kind,
                c.content,
                c.section,
                c.page_start,
                c.page_end,
                c.row_start,
                c.row_end,
                c.char_start,
                c.char_end,
                c.token_estimate,
                d.title,
                d.filename,
                d.format,
                distance.label("distance"),
            )
            .select_from(
                _searchable_join()
                .join(
                    t.document_embeddings,
                    and_(
                        s.document_id == d.id,
                        s.organization_id == d.organization_id,
                        s.embedding_model_id == model_id,
                        s.status == "ready",
                        s.source_generation == i.indexed_generation,
                    ),
                )
                .join(
                    t.document_chunks,
                    and_(c.document_id == d.id, c.organization_id == d.organization_id),
                )
                .join(
                    t.chunk_embeddings,
                    and_(
                        e.chunk_id == c.id,
                        e.organization_id == c.organization_id,
                        e.embedding_model_id == model_id,
                        e.content_sha256 == c.content_sha256,
                        e.dimensions == dims,
                    ),
                )
            )
            .where(visible_documents(query.scope), c.organization_id == query.scope.organization_id)
            .order_by(distance, c.document_id, c.chunk_index)
            .limit(query.limit)
        )
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = {self._ef_search}"))
        rows = (await conn.execute(stmt)).all()
        floor = model.relevance_floor
        passages: list[Passage] = []
        for row in rows:
            similarity = round(1.0 - float(row.distance), 6)
            if floor is not None and similarity < floor:
                continue
            passages.append(
                Passage(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    chunk_index=row.chunk_index,
                    kind=row.kind,
                    content=row.content,
                    section=row.section,
                    page_start=row.page_start,
                    page_end=row.page_end,
                    row_start=row.row_start,
                    row_end=row.row_end,
                    char_start=row.char_start,
                    char_end=row.char_end,
                    token_estimate=row.token_estimate,
                    document_title=row.title or row.filename,
                    filename=row.filename,
                    format=row.format,
                    score=similarity,
                    matched_terms=0,
                    similarity=similarity,
                    vector_rank=len(passages) + 1,
                )
            )
        return passages


async def count_semantic_ready(
    conn: AsyncConnection, query: RetrievalQuery, model: EmbeddingModel
) -> int:
    """How many of the searchable documents in scope are semantically ready for `model`."""
    if model.id is None:
        return 0
    d, i, s = t.documents.c, t.document_indexes.c, t.document_embeddings.c
    model_id: Any = literal_column(f"'{UUID(str(model.id))}'::uuid")
    from sqlalchemy import func

    result = await conn.execute(
        select(func.count())
        .select_from(
            _searchable_join().join(
                t.document_embeddings,
                and_(
                    s.document_id == d.id,
                    s.organization_id == d.organization_id,
                    s.embedding_model_id == model_id,
                    s.status == "ready",
                    s.source_generation == i.indexed_generation,
                ),
            )
        )
        .where(visible_documents(query.scope))
    )
    return int(result.scalar_one())
