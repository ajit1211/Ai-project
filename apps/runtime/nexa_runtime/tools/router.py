"""`/api/v1/tools/*` (docs/api/tools.md). Deciding on a paused action is a conversation route
(`POST /conversations/{id}/approvals/{approval_id}/`), because it continues that reply."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request

from nexa_common.context import current_request_id
from nexa_runtime.auth.dependencies import CurrentPrincipal
from nexa_runtime.responses import Envelope, ok
from nexa_runtime.tools.schemas import InvocationOut, ToolDetailOut, ToolOut, ToolSettingUpdate
from nexa_runtime.tools.service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])

ToolId = Annotated[str, Path(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]{1,63}$")]


def _service(request: Request) -> ToolService:
    service: ToolService = request.app.state.tool_service
    return service


Service = Annotated[ToolService, Depends(_service)]


@router.get("/")
async def list_tools(principal: CurrentPrincipal, service: Service) -> Envelope[list[ToolOut]]:
    """Every tool NEXA has, whether it is switched on here and whether the caller may let AI
    use it."""
    return ok(await service.list_tools(principal))


@router.get("/invocations/{invocation_id}/")
async def get_invocation(
    invocation_id: UUID, principal: CurrentPrincipal, service: Service
) -> Envelope[InvocationOut]:
    return ok(await service.invocation(principal, invocation_id))


@router.get("/{tool_id}/")
async def get_tool(
    tool_id: ToolId, principal: CurrentPrincipal, service: Service
) -> Envelope[ToolDetailOut]:
    return ok(await service.get_tool(principal, tool_id))


@router.patch("/{tool_id}/")
async def update_tool(
    tool_id: ToolId, body: ToolSettingUpdate, principal: CurrentPrincipal, service: Service
) -> Envelope[ToolOut]:
    """Switch a tool on or off for the organization (`ai.tool.manage`)."""
    out = await service.set_enabled(
        principal, tool_id, enabled=body.enabled, request_id=current_request_id()
    )
    return ok(out, message="Tool switched on." if body.enabled else "Tool switched off.")
