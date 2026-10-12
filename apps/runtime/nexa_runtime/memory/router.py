"""`/api/v1/memories/*` (docs/api/memories.md).

Every route needs a verified token and an active membership; the engine applies the scope
rules and the `memory.*` permissions. The fixed paths (`search`, `settings`, `clear`) are
declared before `/{memory_id}/`, so they are never read as ids.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.chat.schemas import NulFree
from nexa_runtime.memory.schemas import (
    ClearMemories,
    ClearResult,
    CreateMemory,
    MemoryKind,
    MemoryOut,
    MemoryScope,
    MemorySettingsOut,
    MemoryStatus,
    SearchHit,
    UpdateMemory,
    UpdateMemorySettings,
)
from nexa_runtime.memory.service import MemoryEngine
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/memories", tags=["memories"])


def _engine(request: Request) -> MemoryEngine:
    engine: MemoryEngine = request.app.state.memory
    return engine


Engine = Annotated[MemoryEngine, Depends(_engine)]


@router.post("/", status_code=201)
async def create_memory(
    body: CreateMemory, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemoryOut]:
    return ok(await engine.create(principal, body))


@router.get("/")
async def list_memories(
    principal: CurrentPrincipal,
    engine: Engine,
    scope: MemoryScope | None = None,
    kind: MemoryKind | None = None,
    status: MemoryStatus | None = None,
    conversation_id: UUID | None = None,
    q: Annotated[NulFree, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Envelope[list[MemoryOut]]:
    items, next_cursor = await engine.list_memories(
        principal,
        scope=scope,
        kind=kind,
        status=status,
        conversation_id=conversation_id,
        query=q,
        limit=limit,
        cursor=cursor,
    )
    return Envelope[list[MemoryOut]](
        data=items, meta={"next_cursor": next_cursor, "has_more": next_cursor is not None}
    )


@router.get("/search/")
async def search_memories(
    principal: CurrentPrincipal,
    engine: Engine,
    q: Annotated[NulFree, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> Envelope[list[SearchHit]]:
    return ok(await engine.search(principal, query=q, limit=limit))


@router.get("/settings/")
async def get_settings(principal: CurrentPrincipal, engine: Engine) -> Envelope[MemorySettingsOut]:
    return ok(await engine.get_settings(principal))


@router.patch("/settings/")
async def update_settings(
    body: UpdateMemorySettings, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemorySettingsOut]:
    return ok(await engine.update_settings(principal, body))


@router.post("/clear/")
async def clear_memories(
    principal: CurrentPrincipal, engine: Engine, body: ClearMemories | None = None
) -> Envelope[ClearResult]:
    result = await engine.clear(principal, body or ClearMemories())
    return ok(result, message=f"{result.deleted} memories deleted.")


@router.get("/{memory_id}/")
async def get_memory(
    memory_id: UUID, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemoryOut]:
    return ok(await engine.get(principal, memory_id))


@router.patch("/{memory_id}/")
async def update_memory(
    memory_id: UUID, body: UpdateMemory, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemoryOut]:
    return ok(await engine.update(principal, memory_id, body))


@router.delete("/{memory_id}/")
async def delete_memory(
    memory_id: UUID, principal: CurrentPrincipal, engine: Engine
) -> Envelope[None]:
    await engine.delete(principal, memory_id)
    return ok(None, message="Memory deleted.")


@router.post("/{memory_id}/approve/")
async def approve_memory(
    memory_id: UUID, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemoryOut]:
    return ok(await engine.approve(principal, memory_id))


@router.post("/{memory_id}/reject/")
async def reject_memory(
    memory_id: UUID, principal: CurrentPrincipal, engine: Engine
) -> Envelope[MemoryOut]:
    return ok(await engine.reject(principal, memory_id))
