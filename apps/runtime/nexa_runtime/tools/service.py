"""The tool API's operations (docs/api/tools.md): what tools exist for the caller, switching
a tool off (or on again) for the organization, and reading one invocation record."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_common.access import Principal, require
from nexa_common.errors import NotFound
from nexa_runtime.agents import repository as agent_repo
from nexa_runtime.chat import repository as chat_repo
from nexa_runtime.tools import repository as repo
from nexa_runtime.tools.activity import invocation_detail
from nexa_runtime.tools.audit import ToolAudit
from nexa_runtime.tools.errors import ToolNotFound
from nexa_runtime.tools.executor import EXECUTE_PERMISSION, ToolExecutor
from nexa_runtime.tools.registry import ToolDefinition
from nexa_runtime.tools.schemas import InvocationOut, ToolDetailOut, ToolOut

MANAGE_PERMISSION = "ai.tool.manage"


class ToolService:
    def __init__(self, *, engine: AsyncEngine, executor: ToolExecutor, audit: ToolAudit) -> None:
        self._engine = engine
        self._executor = executor
        self._audit = audit

    async def list_tools(self, principal: Principal) -> list[ToolOut]:
        switches = await self._executor.switches(_organization(principal))
        return [self._out(d, principal, switches) for d in self._executor.registry.all()]

    async def get_tool(self, principal: Principal, tool_id: str) -> ToolDetailOut:
        definition = self._executor.registry.get(tool_id)
        if definition is None:
            raise ToolNotFound
        switches = await self._executor.switches(_organization(principal))
        return ToolDetailOut(
            **self._out(definition, principal, switches).model_dump(),
            input_schema=definition.input_schema,
            output_schema=definition.output_schema,
            timeout_seconds=definition.timeout_seconds,
            max_retries=definition.max_retries,
        )

    async def set_enabled(
        self, principal: Principal, tool_id: str, *, enabled: bool, request_id: str | None
    ) -> ToolOut:
        """Switch a tool on or off for the caller's organization (`ai.tool.manage`). A tool
        switched off for the whole platform stays off."""
        require(principal, MANAGE_PERMISSION)
        definition = self._executor.registry.get(tool_id)
        if definition is None:
            raise ToolNotFound
        organization_id = _organization(principal)
        async with self._engine.begin() as conn:
            await repo.set_organization_switch(
                conn, organization_id, tool_id, enabled=enabled, user_id=principal.user_id
            )
        await self._audit.record(
            "TOOL_SETTINGS_CHANGED",
            organization_id=organization_id,
            actor_id=principal.user_id,
            resource_type="tool",
            resource_id=tool_id,
            request_id=request_id,
            metadata={
                "enabled": enabled,
                "scope": "organization",
                "tool_version": definition.version,
            },
        )
        switches = await self._executor.switches(organization_id)
        return self._out(definition, principal, switches)

    async def invocation(self, principal: Principal, invocation_id: UUID) -> InvocationOut:
        """One invocation record, for the owner of its conversation only (conversations are
        private), with document results re-authorized now."""
        organization_id = _organization(principal)
        async with self._engine.connect() as conn:
            row = await repo.invocation(conn, organization_id, invocation_id)
            if row is None:
                raise NotFound
            if row.run_id is not None:
                run = await agent_repo.get_run(conn, organization_id, row.run_id)
                if run is None or run.user_id != principal.user_id:
                    raise NotFound
            else:
                conversation = await chat_repo.get_conversation(
                    conn, organization_id, row.conversation_id
                )
                if conversation is None or conversation.owner_id != principal.user_id:
                    raise NotFound
            return await invocation_detail(conn, principal, self._executor.registry, row)

    def _out(
        self,
        definition: ToolDefinition,
        principal: Principal,
        switches: dict[str, tuple[bool, bool]],
    ) -> ToolOut:
        platform, organization = switches.get(definition.id, (True, True))
        enabled = definition.enabled and platform and organization
        disabled_by = None
        if not (definition.enabled and platform):
            disabled_by = "platform"
        elif not organization:
            disabled_by = "organization"
        return ToolOut(
            id=definition.id,
            name=definition.name,
            description=definition.description,
            version=definition.version,
            category=definition.category,
            risk=definition.risk,
            read_only=definition.read_only,
            requires_approval=definition.requires_approval,
            enabled=enabled,
            available=enabled
            and principal.has(EXECUTE_PERMISSION)
            and definition.allowed_for(principal),
            disabled_by=disabled_by,
            permissions=sorted(definition.permissions | {EXECUTE_PERMISSION}),
        )


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id
