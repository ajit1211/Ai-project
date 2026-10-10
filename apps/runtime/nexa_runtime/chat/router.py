"""`/api/v1/conversations/*` (docs/api/conversations.md).

Every route needs a verified token and an active membership; the service applies the
ownership rule and the `conversation.*` permissions. Sending a message answers with an SSE
stream when the client asks for `text/event-stream`, otherwise with JSON once the reply is
complete.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from nexa_ai.sse import SSE_HEADERS, SSE_MEDIA_TYPE, sse_bytes
from nexa_common.context import current_request_id
from nexa_runtime.ai.router import SSEResponse
from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.chat.events import PROTOCOL, PROTOCOL_HEADER
from nexa_runtime.chat.schemas import (
    CancelResult,
    ConversationOut,
    CreateConversation,
    Exchange,
    MessageOut,
    NulFree,
    Regenerate,
    SendMessage,
    UpdateConversation,
)
from nexa_runtime.chat.service import ChatService, Generation
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _service(request: Request) -> ChatService:
    service: ChatService = request.app.state.chat
    return service


Service = Annotated[ChatService, Depends(_service)]


def _wants_stream(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "")


@router.post("/", status_code=201)
async def create_conversation(
    body: CreateConversation, principal: CurrentPrincipal, service: Service
) -> Envelope[ConversationOut]:
    return ok(await service.create(principal, body))


@router.get("/")
async def list_conversations(
    principal: CurrentPrincipal,
    service: Service,
    status: Literal["active", "archived"] = "active",
    q: Annotated[NulFree, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Envelope[list[ConversationOut]]:
    items, next_cursor = await service.list_conversations(
        principal, status=status, query=q, limit=limit, cursor=cursor
    )
    return Envelope[list[ConversationOut]](
        data=items, meta={"next_cursor": next_cursor, "has_more": next_cursor is not None}
    )


@router.get("/{conversation_id}/")
async def get_conversation(
    conversation_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[ConversationOut]:
    return ok(await service.get(principal, conversation_id))


@router.patch("/{conversation_id}/")
async def update_conversation(
    conversation_id: UUID,
    body: UpdateConversation,
    principal: CurrentPrincipal,
    service: Service,
) -> Envelope[ConversationOut]:
    return ok(await service.update(principal, conversation_id, body))


@router.delete("/{conversation_id}/")
async def delete_conversation(
    conversation_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[None]:
    await service.delete(principal, conversation_id)
    return ok(None, message="Conversation deleted.")


@router.get("/{conversation_id}/messages/")
async def list_messages(
    conversation_id: UUID,
    principal: CurrentPrincipal,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> Envelope[list[MessageOut]]:
    items, next_before = await service.list_messages(
        principal, conversation_id, limit=limit, before=before
    )
    return Envelope[list[MessageOut]](
        data=items, meta={"next_before": next_before, "has_more": next_before is not None}
    )


@router.post("/{conversation_id}/messages/", response_model=None)
async def send_message(
    conversation_id: UUID,
    body: SendMessage,
    request: Request,
    principal: CurrentPrincipal,
    service: Service,
) -> Response:
    outcome = await service.send(principal, conversation_id, body, request_id=current_request_id())
    if isinstance(outcome, Exchange):
        return _json(outcome, status_code=200)  # the earlier submission, not a new reply
    return await _respond(outcome, request, service)


@router.post("/{conversation_id}/regenerate/", response_model=None)
async def regenerate(
    conversation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    service: Service,
    body: Regenerate | None = None,
) -> Response:
    generation = await service.regenerate(
        principal, conversation_id, body or Regenerate(), request_id=current_request_id()
    )
    return await _respond(generation, request, service)


@router.post("/{conversation_id}/cancel/")
async def cancel(
    conversation_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[CancelResult]:
    return ok(await service.cancel(principal, conversation_id))


async def _respond(generation: Generation, request: Request, service: ChatService) -> Response:
    if _wants_stream(request):
        events = service.stream(generation)
        return SSEResponse(
            sse_bytes(events),
            source=events,
            media_type=SSE_MEDIA_TYPE,
            headers={**SSE_HEADERS, PROTOCOL_HEADER: PROTOCOL},
        )
    return _json(await service.complete(generation), status_code=201)


def _json(exchange: Exchange, *, status_code: int) -> JSONResponse:
    body: dict[str, Any] = Envelope[Exchange](data=exchange).model_dump(mode="json")
    return JSONResponse(body, status_code=status_code)
