"""Finding the passages that answer a question, inside what the caller may see
(docs/architecture/rag.md#retrieval).

The Phase 08 baseline is lexical: PostgreSQL full-text search over `document_chunks.search`
(the `english` configuration, which stems words and drops stop words; the section path is
weighted A and the passage text B).

    question ─► terms: the words of the question minus stop words, at most 16
             ─► candidates: chunks containing at least `min_matched_terms` of the terms, from
                documents that are processed, indexed, not deleted, in the caller's
                organization and visible to the caller, within the requested scope
             ─► score = share of the terms the chunk contains + ts_rank_cd (normalized to 0..1)
             ─► ordered by score, then document and position, so ties are deterministic

Authorization is part of the query, not a filter applied to its results: a chunk of a document
the caller may not see is never read. The visibility predicate is the shared policy
(`nexa_common.access.resource_access`) written as SQL; a test holds the two in agreement.

The score orders passages for one question; it is not a probability and not a confidence
measure. Lexical search finds words, not meaning: "car" does not match "automobile", and a
question phrased in other words than the document finds nothing. Phase 09 adds a vector
retriever behind the same `Retriever` interface.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Float, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from nexa_runtime.rag import tables as t

SEARCH_CONFIG = "english"
MAX_TERMS = 16
NEAR_DUPLICATE = 0.8

# Words and numbers in any script, at least two characters; underscores and every tsquery
# operator character are excluded, so a term is always safe inside to_tsquery().
_WORD = re.compile(r"[^\W_]{2,}")

# PostgreSQL's `english` stop words (snowball english.stop). Dropping them here keeps
# to_tsquery() from ever receiving a query with no lexemes.
_STOP_WORD_TEXT = (
    "i me my myself we our ours ourselves you your yours yourself yourselves he him his "
    "himself she her hers herself it its itself they them their theirs themselves what "
    "which who whom this that these those am is are was were be been being have has had "
    "having do does did doing a an the and but if or because as until while of at by for "
    "with about against between into through during before after above below to from up "
    "down in out on off over under again further then once here there when where why how "
    "all any both each few more most other some such no nor not only own same so than too "
    "very s t can will just don should now"
)
STOP_WORDS = frozenset(_STOP_WORD_TEXT.split())


def query_terms(text: str) -> tuple[str, ...]:
    """The searchable words of a question, lowercased, in order, without repeats."""
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word in STOP_WORDS or word in terms:
            continue
        terms.append(word)
        if len(terms) == MAX_TERMS:
            break
    return tuple(terms)


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    """Which documents may be searched. Built from the caller's principal (organization from
    the token, permissions and teams from the database), never from client input alone."""

    organization_id: UUID
    user_id: UUID
    team_ids: frozenset[UUID]
    manage_all: bool
    # Narrowing within what is visible: an explicit selection, or one team's documents.
    document_ids: tuple[UUID, ...] | None = None
    team_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    terms: tuple[str, ...]
    scope: DocumentFilter
    limit: int
    min_matched_terms: int = 1


@dataclass(frozen=True, slots=True)
class Passage:
    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    kind: str
    content: str
    section: str
    page_start: int | None
    page_end: int | None
    row_start: int | None
    row_end: int | None
    char_start: int
    char_end: int
    token_estimate: int
    document_title: str
    filename: str
    format: str
    score: float
    matched_terms: int


class Retriever(Protocol):
    """A way of finding passages. Implementations must apply `query.scope` inside their own
    query: results are given to the model as they are returned."""

    name: str

    async def search(self, conn: AsyncConnection, query: RetrievalQuery) -> list[Passage]: ...


def visible_documents(scope: DocumentFilter) -> ColumnElement[bool]:
    """Documents the caller may read and that are ready to search, as a SQL predicate on
    `documents` joined with `document_indexes`."""
    d, i = t.documents.c, t.document_indexes.c
    clauses: list[ColumnElement[bool]] = [
        d.organization_id == scope.organization_id,
        d.deleted_at.is_(None),
        d.status == "completed",
        i.indexed_at.is_not(None),
        i.chunk_count > 0,
    ]
    if not scope.manage_all:
        allowed: list[ColumnElement[bool]] = [
            d.uploaded_by_id == scope.user_id,
            d.visibility == "organization",
        ]
        if scope.team_ids:
            allowed.append(and_(d.visibility == "team", d.team_id.in_(sorted(scope.team_ids))))
        clauses.append(or_(*allowed))
    if scope.document_ids is not None:
        clauses.append(d.id.in_(list(scope.document_ids)))
    if scope.team_id is not None:
        clauses.append(and_(d.visibility == "team", d.team_id == scope.team_id))
    return and_(*clauses)


def _searchable_join() -> Any:
    return t.documents.join(
        t.document_indexes,
        and_(
            t.document_indexes.c.document_id == t.documents.c.id,
            t.document_indexes.c.organization_id == t.documents.c.organization_id,
        ),
    )


async def count_searchable(conn: AsyncConnection, scope: DocumentFilter) -> int:
    result = await conn.execute(
        select(func.count()).select_from(_searchable_join()).where(visible_documents(scope))
    )
    return int(result.scalar_one())


class LexicalRetriever:
    """PostgreSQL full-text search (the Phase 08 baseline)."""

    name = "postgres-fts/v1"

    async def search(self, conn: AsyncConnection, query: RetrievalQuery) -> list[Passage]:
        if not query.terms or query.limit <= 0:
            return []
        c, d = t.document_chunks.c, t.documents.c
        any_term = func.to_tsquery(SEARCH_CONFIG, " | ".join(query.terms))
        matched = sum(
            (
                case((c.search.op("@@")(func.to_tsquery(SEARCH_CONFIG, term)), 1), else_=0)
                for term in query.terms
            ),
            start=literal(0),
        )
        score = cast(matched, Float) / len(query.terms) + func.ts_rank_cd(c.search, any_term, 32)
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
                matched.label("matched"),
                score.label("score"),
            )
            .select_from(
                _searchable_join().join(
                    t.document_chunks,
                    and_(
                        c.document_id == d.id,
                        c.organization_id == d.organization_id,
                    ),
                )
            )
            .where(
                visible_documents(query.scope),
                c.organization_id == query.scope.organization_id,
                c.search.op("@@")(any_term),
                matched >= query.min_matched_terms,
            )
            .order_by(score.desc(), c.document_id, c.chunk_index)
            .limit(query.limit)
        )
        rows = (await conn.execute(stmt)).all()
        return [
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
                score=round(float(row.score), 6),
                matched_terms=int(row.matched),
            )
            for row in rows
        ]


def drop_near_duplicates(
    passages: Sequence[Passage], *, threshold: float = NEAR_DUPLICATE
) -> list[Passage]:
    """Keep the better-ranked of two passages whose words mostly coincide (the same notice
    in two documents, overlapping chunks), so repeats do not crowd out other evidence."""
    kept: list[Passage] = []
    signatures: list[frozenset[str]] = []
    for passage in passages:
        words = frozenset(_WORD.findall(passage.content.lower()))
        if any(_jaccard(words, other) >= threshold for other in signatures):
            continue
        kept.append(passage)
        signatures.append(words)
    return kept


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)
