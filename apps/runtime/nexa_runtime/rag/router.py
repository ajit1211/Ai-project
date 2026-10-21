"""`GET /api/v1/ai/retrieval/`: which ways of searching documents this deployment supports
(docs/api/conversations.md#document-grounded-answers).

For the chat's search-mode control: only modes that can run here are offered. No model
internals (provider, dimensions) are returned to ordinary users; administrators see those in
Core's `GET /api/v1/documents/search-status/`.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from nexa_common.access import require
from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.rag.service import RagEngine
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/ai", tags=["retrieval"])

Mode = Literal["lexical", "semantic", "hybrid"]


class RetrievalOptions(BaseModel):
    modes: list[Mode]
    default_mode: Mode
    semantic_available: bool
    # A stable code when semantic search does not run here: disabled, pgvector_missing,
    # no_model, provider_not_configured.
    semantic_unavailable_reason: str | None
    embedding_model: str | None


@router.get("/retrieval/")
async def retrieval_options(
    request: Request, principal: CurrentPrincipal
) -> Envelope[RetrievalOptions]:
    require(principal, "documents.view")
    rag: RagEngine = request.app.state.rag
    status = await rag.semantic.status()
    configured: Mode = request.app.state.settings.rag_retrieval_mode
    if status.available:
        modes: list[Mode] = ["hybrid", "semantic", "lexical"]
        default: Mode = configured
    else:
        modes, default = ["lexical"], "lexical"
    return ok(
        RetrievalOptions(
            modes=modes,
            default_mode=default,
            semantic_available=status.available,
            semantic_unavailable_reason=None if status.available else status.reason,
            embedding_model=status.model.key if status.available and status.model else None,
        )
    )
