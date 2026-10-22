"""`/api/v1/ai/*` on Runtime: the models this organization may use, chat, and streamed chat.

`GET /api/v1/ai/usage/` is served by Core, which owns the usage records.

Every route needs a verified token, an active membership in the token's organization and
the `ai.chat` permission; the gateway checks the permission again so no other caller (an
agent run in Phase 11) can skip it. Identity and organization come from the token and the
database only; the request body cannot name either.
"""

from collections.abc import AsyncIterable, Mapping
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIInvalidRequest
from nexa_ai.gateway import Gateway
from nexa_ai.sse import SSE_HEADERS, SSE_MEDIA_TYPE, sse_bytes
from nexa_ai.types import ChatRequest, ChatResponse, Feature
from nexa_common.access import Principal
from nexa_common.context import current_request_id
from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/ai", tags=["ai"])


def _gateway(request: Request) -> Gateway:
    gateway: Gateway = request.app.state.gateway
    return gateway


# Features a client may claim on the raw AI routes; `chat` is set only by the chat engine.
_CLIENT_FEATURES = frozenset({Feature.API, Feature.PLAYGROUND})


def _check_feature(body: ChatRequest) -> None:
    if body.feature not in _CLIENT_FEATURES:
        raise AIInvalidRequest("This feature label is reserved for NEXA's own services.")
    # Tools are offered by NEXA's tool registry, per user and per call, in conversations
    # (docs/architecture/tools.md); a raw request cannot define or replay its own.
    if body.offers_tools or body.has_tool_history or body.tool_choice != "auto":
        raise AIInvalidRequest("Tool calling is available in conversations only.")


def _context(principal: Principal) -> AIRequestContext:
    return AIRequestContext.from_principal(principal, request_id=current_request_id())


GatewayDep = Annotated[Gateway, Depends(_gateway)]


class ModelInfo(BaseModel):
    name: str
    display_name: str
    description: str
    is_default: bool
    capabilities: list[str]
    context_window: int
    max_output_tokens: int


class SSEResponse(StreamingResponse):
    """Always closes the event stream, even when the client disconnects or the task is
    cancelled, so the call's outcome and usage are recorded.

    `source` is the stream the SSE bytes are made from; it is closed as well. That matters
    when the bytes were never read at all (the client left before the response started):
    closing an async generator that never started runs none of its cleanup, so closing the
    encoder alone would never reach the source."""

    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        source: object = None,
        media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                for stream in (self.body_iterator, self._source):
                    aclose = getattr(stream, "aclose", None)
                    if aclose is not None:
                        await aclose()


@router.get("/models/")
async def list_models(
    principal: CurrentPrincipal, gateway: GatewayDep
) -> Envelope[list[ModelInfo]]:
    aliases = await gateway.available_models(_context(principal))
    infos = []
    for alias in aliases:
        # Describe only the targets this process can actually route to.
        usable = [t for t in alias.targets if t.is_active and t.provider in gateway.providers]
        infos.append(
            ModelInfo(
                name=alias.name,
                display_name=alias.display_name,
                description=alias.description,
                is_default=alias.is_default,
                capabilities=sorted(
                    c.value for c in frozenset().union(*(t.capabilities for t in usable))
                ),
                context_window=min(t.context_window for t in usable),
                max_output_tokens=min(
                    min(t.max_output_tokens for t in usable), gateway.max_output_tokens_cap
                ),
            )
        )
    return ok(infos)


@router.post("/chat/")
async def chat(
    body: ChatRequest, principal: CurrentPrincipal, gateway: GatewayDep
) -> Envelope[ChatResponse]:
    _check_feature(body)
    return ok(await gateway.chat(body, _context(principal)))


@router.post("/chat/stream/", response_class=SSEResponse)
async def chat_stream(
    body: ChatRequest, principal: CurrentPrincipal, gateway: GatewayDep
) -> SSEResponse:
    # Policy, model and quota errors raise here and become normal JSON errors. Once the
    # response starts, every failure is an `error` event in the stream.
    _check_feature(body)
    events = await gateway.open_stream(body, _context(principal))
    return SSEResponse(
        sse_bytes(events), source=events, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )
