"""Combining lexical and semantic results, and the (optional) reranking step
(docs/architecture/vector-search.md#hybrid-retrieval).

Reciprocal Rank Fusion: a passage's fused score is the sum, over the methods that returned it,
of 1 / (k + its rank in that method's list). Only ranks are combined, never the raw scores:
a full-text rank and a cosine similarity are on unrelated scales. A passage both methods found
rises; one only one method found keeps a place. Ties are broken by the best single rank, then
by document and position, so the order is deterministic. k (default 60, the value from the
original RRF paper) dampens the difference between the first ranks.

Reranking: an interface only. No reranker is enabled in this phase (none could be configured
and evaluated here), so the fused order is final; a later reranker receives a bounded
candidate list, may only reorder it (never add passages), and falls back to the fused order on
failure or timeout.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from nexa_common.logs import get_logger
from nexa_runtime.rag.retrieval import Passage

log = get_logger("nexa.runtime.rag.fusion")


def reciprocal_rank_fusion(
    ranked: Mapping[str, Sequence[Passage]], *, k: int = 60, limit: int | None = None
) -> list[Passage]:
    """Fuse ranked lists keyed by method ("lexical", "semantic"); deduplicated by chunk id."""
    scores: dict[UUID, float] = {}
    ranks: dict[UUID, dict[str, int]] = {}
    found: dict[UUID, dict[str, Passage]] = {}
    for method, passages in ranked.items():
        for rank, passage in enumerate(passages, start=1):
            chunk = passage.chunk_id
            if method in ranks.get(chunk, {}):
                continue  # a list should not repeat a chunk; keep its best rank
            scores[chunk] = scores.get(chunk, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(chunk, {})[method] = rank
            found.setdefault(chunk, {})[method] = passage
    order = sorted(
        scores,
        key=lambda chunk: (
            -scores[chunk],
            min(ranks[chunk].values()),
            str(next(iter(found[chunk].values())).document_id),
            next(iter(found[chunk].values())).chunk_index,
        ),
    )
    fused: list[Passage] = []
    for chunk in order[:limit] if limit is not None else order:
        lexical = found[chunk].get("lexical")
        semantic = found[chunk].get("semantic")
        base = lexical or semantic
        assert base is not None  # noqa: S101 - every chunk came from some list
        fused.append(
            replace(
                base,
                score=round(scores[chunk], 6),
                matched_terms=lexical.matched_terms if lexical else 0,
                similarity=semantic.similarity if semantic else None,
                lexical_rank=ranks[chunk].get("lexical"),
                vector_rank=ranks[chunk].get("semantic"),
            )
        )
    return fused


class Reranker(Protocol):
    name: str

    async def rerank(self, question: str, passages: Sequence[Passage]) -> list[Passage]: ...


class NoReranker:
    """The default: the fused order is final."""

    name = "none"

    async def rerank(self, question: str, passages: Sequence[Passage]) -> list[Passage]:
        return list(passages)


async def rerank_safely(
    reranker: Reranker, question: str, passages: Sequence[Passage], *, timeout_seconds: float
) -> tuple[list[Passage], bool]:
    """The reranked list, or the input list when the reranker fails, times out, or returns
    anything but a reordering of its input. Returns (passages, reranked)."""
    if isinstance(reranker, NoReranker) or len(passages) < 2:
        return list(passages), False
    try:
        async with asyncio.timeout(timeout_seconds):
            reordered = await reranker.rerank(question, passages)
    except Exception as exc:
        log.warning("rag.rerank.failed", reranker=reranker.name, error=type(exc).__name__)
        return list(passages), False
    if sorted(p.chunk_id for p in reordered) != sorted(p.chunk_id for p in passages):
        log.warning("rag.rerank.rejected", reranker=reranker.name, reason="changed_candidates")
        return list(passages), False
    return reordered, True
