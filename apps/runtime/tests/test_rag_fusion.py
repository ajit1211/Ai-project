"""Reciprocal rank fusion, the reranking guard and the choice of retrieval methods, without a
database (docs/architecture/vector-search.md#hybrid-retrieval)."""

import asyncio
from collections.abc import Sequence
from uuid import UUID

import pytest

from nexa_runtime.rag.fusion import NoReranker, reciprocal_rank_fusion, rerank_safely
from nexa_runtime.rag.retrieval import Passage
from nexa_runtime.rag.service import _methods

DOC_A = UUID("00000000-0000-7000-8000-00000000000a")
DOC_B = UUID("00000000-0000-7000-8000-00000000000b")


def passage(
    n: int,
    document: UUID = DOC_A,
    *,
    score: float = 1.0,
    similarity: float | None = None,
    matched: int = 0,
) -> Passage:
    return Passage(
        chunk_id=UUID(int=n),
        document_id=document,
        chunk_index=n,
        kind="text",
        content=f"passage {n}",
        section="",
        page_start=None,
        page_end=None,
        row_start=None,
        row_end=None,
        char_start=0,
        char_end=10,
        token_estimate=3,
        document_title="Doc",
        filename="doc.md",
        format="md",
        score=score,
        matched_terms=matched,
        similarity=similarity,
    )


def ids(passages: Sequence[Passage]) -> list[int]:
    return [p.chunk_id.int for p in passages]


class TestFusion:
    def test_scores_are_summed_reciprocal_ranks(self) -> None:
        fused = reciprocal_rank_fusion(
            {
                "lexical": [passage(1, matched=3), passage(2, matched=1)],
                "semantic": [passage(2, similarity=0.8), passage(3, similarity=0.5)],
            },
            k=60,
        )
        assert ids(fused) == [2, 1, 3]  # found by both, then lexical #1, then semantic #2
        both, lexical_only, semantic_only = fused
        assert both.score == round(1 / 62 + 1 / 61, 6)
        assert (both.lexical_rank, both.vector_rank) == (2, 1)
        assert (both.matched_terms, both.similarity) == (1, 0.8)  # each method's own facts
        assert (lexical_only.lexical_rank, lexical_only.vector_rank) == (1, None)
        assert lexical_only.similarity is None
        assert (semantic_only.lexical_rank, semantic_only.vector_rank) == (None, 2)
        assert semantic_only.matched_terms == 0

    def test_each_chunk_appears_once(self) -> None:
        repeated = [passage(1), passage(1), passage(2)]
        fused = reciprocal_rank_fusion({"lexical": repeated, "semantic": [passage(1)]})
        assert ids(fused) == [1, 2]
        assert fused[0].lexical_rank == 1  # the best rank of the repeated chunk

    def test_ties_are_broken_deterministically(self) -> None:
        # Same fused score (each first in one list): best single rank ties too, so the
        # document id decides, then the position.
        first = reciprocal_rank_fusion(
            {"lexical": [passage(5, DOC_B)], "semantic": [passage(4, DOC_A)]}
        )
        second = reciprocal_rank_fusion(
            {"semantic": [passage(4, DOC_A)], "lexical": [passage(5, DOC_B)]}
        )
        assert ids(first) == ids(second) == [4, 5]
        same_document = reciprocal_rank_fusion({"lexical": [passage(9)], "semantic": [passage(7)]})
        assert ids(same_document) == [7, 9]

    def test_limit_and_k(self) -> None:
        lists = {"lexical": [passage(n) for n in range(1, 6)], "semantic": []}
        assert ids(reciprocal_rank_fusion(lists, limit=2)) == [1, 2]
        # A smaller k widens the gap between the first ranks.
        top = reciprocal_rank_fusion(lists, k=1)
        assert top[0].score == round(1 / 2, 6)

    def test_one_empty_list_keeps_the_other_order(self) -> None:
        fused = reciprocal_rank_fusion({"lexical": [], "semantic": [passage(3), passage(1)]})
        assert ids(fused) == [3, 1]


class Scripted:
    name = "scripted"

    def __init__(
        self, result: list[Passage] | None = None, *, delay: float = 0.0, error: bool = False
    ) -> None:
        self.result, self.delay, self.error = result, delay, error

    async def rerank(self, question: str, passages: Sequence[Passage]) -> list[Passage]:
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("model unavailable")
        return list(self.result if self.result is not None else reversed(passages))


@pytest.mark.anyio
class TestReranking:
    candidates = (passage(1), passage(2), passage(3))

    async def test_no_reranker_keeps_the_fused_order(self) -> None:
        result, reranked = await rerank_safely(
            NoReranker(), "q", self.candidates, timeout_seconds=1
        )
        assert (ids(result), reranked) == ([1, 2, 3], False)

    async def test_a_reordering_is_accepted(self) -> None:
        result, reranked = await rerank_safely(Scripted(), "q", self.candidates, timeout_seconds=1)
        assert (ids(result), reranked) == ([3, 2, 1], True)

    @pytest.mark.parametrize(
        "reranker",
        [
            Scripted([passage(1), passage(2), passage(9)]),  # a passage that was not given
            Scripted([passage(1), passage(1), passage(2)]),  # a repeat in place of one
            Scripted([passage(1)]),  # dropped candidates
            Scripted(error=True),
            Scripted(delay=0.5),  # too slow
        ],
    )
    async def test_anything_else_falls_back_to_the_fused_order(self, reranker: Scripted) -> None:
        result, reranked = await rerank_safely(reranker, "q", self.candidates, timeout_seconds=0.1)
        assert (ids(result), reranked) == ([1, 2, 3], False)


class TestMethods:
    """(lexical, semantic, fallback) for each requested mode, deployment default and
    availability."""

    @pytest.mark.parametrize(
        ("requested", "default", "available", "expected"),
        [
            ("auto", "hybrid", True, (True, True, None)),
            ("auto", "semantic", True, (False, True, None)),
            ("auto", "lexical", True, (True, False, None)),
            ("auto", "hybrid", False, (True, False, None)),  # quietly keyword search
            ("lexical", "hybrid", False, (True, False, None)),
            ("semantic", "lexical", True, (False, True, None)),
            ("hybrid", "lexical", True, (True, True, None)),
            ("semantic", "hybrid", False, (True, False, "semantic_pgvector_missing")),
            ("hybrid", "hybrid", False, (True, False, "semantic_pgvector_missing")),
        ],
    )
    def test_matrix(
        self,
        requested: str,
        default: str,
        available: bool,
        expected: tuple[bool, bool, str | None],
    ) -> None:
        assert _methods(requested, default, available, "pgvector_missing") == expected
