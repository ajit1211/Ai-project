"""Request and response shapes of document-grounded answers (docs/api/conversations.md).

A message is grounded when it is sent with `grounding`; the reply then carries `grounding`
(what was asked for and how it went) and `sources` (the passages given to the model, with
whether the reply cited each one). Nothing the client sends here is trusted: document and team
ids are authorized again on the server, the organization comes from the token, and chunk ids
are never accepted from a client at all.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_DOCUMENTS = 20
MAX_SOURCES = 12

GroundingScopeKind = Literal["documents", "team", "library"]
GroundingOutcome = Literal["pending", "answered", "uncited", "no_evidence", "failed", "cancelled"]
RetrievalMode = Literal["auto", "lexical", "semantic", "hybrid"]


class GroundingRequest(BaseModel):
    """Answer from documents: the selected ones, a team's, or every document the user may
    see (`all_documents`). Exactly one of the three."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["documents"] = "documents"
    document_ids: list[UUID] = Field(default_factory=list, max_length=MAX_DOCUMENTS)
    team_id: UUID | None = None
    all_documents: bool = False
    max_sources: int | None = Field(default=None, ge=1, le=MAX_SOURCES)
    # Phase 09: how to search. "auto" uses the deployment's default (hybrid where semantic
    # search runs, keyword search otherwise).
    retrieval_mode: RetrievalMode = "auto"


class GroundingOut(BaseModel):
    """What a grounded reply was asked to use and how retrieval and citation went."""

    mode: Literal["documents"] = "documents"
    scope: GroundingScopeKind
    document_ids: list[UUID] = Field(default_factory=list)
    team_id: UUID | None = None
    outcome: GroundingOutcome = "pending"
    searched_documents: int = 0
    retrieved: int = 0
    supplied: int = 0
    cited: int = 0
    invalid_citations: list[str] = Field(default_factory=list)
    retrieval_ms: int | None = None
    strategy: str = ""
    # Phase 09: what was asked for, what ran ("lexical", "semantic", "hybrid"), and why it
    # differs when it does (e.g. "query_embedding_failed", "semantic_pgvector_missing").
    retrieval_requested: RetrievalMode = "auto"
    retrieval_mode: Literal["lexical", "semantic", "hybrid"] = "lexical"
    fallback: str | None = None

    @classmethod
    def from_record(cls, record: Any) -> "GroundingOut | None":
        if not isinstance(record, dict):
            return None
        return cls.model_validate({k: v for k, v in record.items() if k in cls.model_fields})


class SourceOut(BaseModel):
    """One passage given to the model. When the document has since been deleted or is no
    longer shared with the reader, only the label, rank and citation flag remain."""

    label: str
    rank: int
    cited: bool
    available: bool
    score: float | None = None
    document_id: UUID | None = None
    chunk_id: UUID | None = None
    document_title: str | None = None
    filename: str | None = None
    format: str | None = None
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    excerpt: str | None = None
    excerpt_truncated: bool = False
