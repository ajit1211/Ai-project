"""`/api/v1/runs/*` (docs/api/agents.md#runs): agent tasks, served by Runtime, which executes
them. Agent definitions are Core's `/api/v1/agents/`."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from nexa_ai.sse import SSE_HEADERS, SSE_MEDIA_TYPE, sse_bytes
from nexa_common.context import current_request_id
from nexa_runtime.agents.schemas import CreateRun, RunControlOut, RunDecision, RunOut, StepOut
from nexa_runtime.agents.service import AgentService
from nexa_runtime.ai.router import SSEResponse
from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/runs", tags=["agent runs"])


def _service(request: Request) -> AgentService:
    service: AgentService = request.app.state.agents
    return service


Service = Annotated[AgentService, Depends(_service)]

RunStatusFilter = Literal[
    "active",
    "finished",
    "queued",
    "running",
    "waiting_for_approval",
    "paused",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]


@router.post("/", response_model=None)
async def create_run(body: CreateRun, principal: CurrentPrincipal, service: Service) -> Response:
    """Submit a task: 202 with the queued run, or 200 with the earlier run of the same
    idempotency key."""
    out, created = await service.submit(principal, body, request_id=current_request_id())
    payload = Envelope[RunOut](data=out, message="Task queued." if created else None)
    return JSONResponse(payload.model_dump(mode="json"), status_code=202 if created else 200)


@router.get("/")
async def list_runs(
    principal: CurrentPrincipal,
    service: Service,
    status: RunStatusFilter | None = None,
    agent_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Envelope[list[RunOut]]:
    items, next_cursor = await service.list_runs(
        principal, status=status, agent_id=agent_id, limit=limit, cursor=cursor
    )
    return Envelope[list[RunOut]](
        data=items, meta={"next_cursor": next_cursor, "has_more": next_cursor is not None}
    )


@router.get("/{run_id}/")
async def get_run(run_id: UUID, principal: CurrentPrincipal, service: Service) -> Envelope[RunOut]:
    return ok(await service.get_run(principal, run_id))


@router.get("/{run_id}/steps/")
async def list_steps(
    run_id: UUID,
    principal: CurrentPrincipal,
    service: Service,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Envelope[list[StepOut]]:
    """The run's history, oldest first; `after` a sequence number for the next page."""
    return ok(await service.steps(principal, run_id, after=after, limit=limit))


@router.get("/{run_id}/events/")
async def run_events(run_id: UUID, principal: CurrentPrincipal, service: Service) -> Response:
    """Progress as Server-Sent Events: `run`, `step` and a final `end`."""
    await service.get_run(principal, run_id)  # 404 before the stream starts
    events = service.events(principal, run_id)
    return SSEResponse(
        sse_bytes(events), source=events, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post("/{run_id}/pause/")
async def pause_run(
    run_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[RunControlOut]:
    out = await service.pause(principal, run_id, request_id=current_request_id())
    return ok(out, message=out.message)


@router.post("/{run_id}/resume/")
async def resume_run(
    run_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[RunControlOut]:
    out = await service.resume(principal, run_id, request_id=current_request_id())
    return ok(out, message=out.message)


@router.post("/{run_id}/cancel/")
async def cancel_run(
    run_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[RunControlOut]:
    out = await service.cancel(principal, run_id, request_id=current_request_id())
    return ok(out, message=out.message)


@router.post("/{run_id}/approvals/{approval_id}/")
async def decide(
    run_id: UUID,
    approval_id: UUID,
    body: RunDecision,
    principal: CurrentPrincipal,
    service: Service,
) -> Envelope[RunOut]:
    """Approve or reject the action the run is waiting for; the run continues."""
    out = await service.decide(
        principal, run_id, approval_id, body, request_id=current_request_id()
    )
    return ok(out, message="Approved." if body.decision == "approve" else "Rejected.")
