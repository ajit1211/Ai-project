"""`workspace_info`: the organization the user is working in, their role and their teams.

Read-only and scoped to the caller's own membership: it never lists other people or other
organizations. The member count is included only for members who may see the member list.
NEXA's workspaces are organizations and teams; there is no separate project entity.
"""

from typing import Any

from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_runtime.db import tables as identity
from nexa_runtime.tools.registry import ToolContext, ToolDefinition, ToolFailure, ToolOutput

_metadata = MetaData()
_teams = Table(
    "teams",
    _metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("name", String, nullable=False),
)
_roles = Table(
    "roles",
    _metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("key", String, nullable=False),
    Column("name", String, nullable=False),
)
_organizations = Table(
    "organizations",
    _metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)


def make_workspace_tool(*, engine: AsyncEngine) -> ToolDefinition:
    async def workspace_info(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
        principal = context.principal
        organization_id = context.organization_id
        async with engine.connect() as conn:
            organization = (
                await conn.execute(
                    select(_organizations.c.name).where(_organizations.c.id == organization_id)
                )
            ).first()
            if organization is None:
                raise ToolFailure("unavailable", "The organization is not available.")
            role = (
                await conn.execute(
                    select(_roles.c.name).where(
                        _roles.c.organization_id == organization_id,
                        _roles.c.key == (principal.role_key or ""),
                    )
                )
            ).first()
            teams = (
                await conn.execute(
                    select(_teams.c.id, _teams.c.name)
                    .where(
                        _teams.c.organization_id == organization_id,
                        _teams.c.id.in_(sorted(principal.team_ids)),
                    )
                    .order_by(_teams.c.name)
                    .limit(50)
                )
            ).all()
            members = None
            if principal.has("members.view"):
                members = (
                    await conn.execute(
                        select(func.count()).where(
                            identity.organization_memberships.c.organization_id == organization_id,
                            identity.organization_memberships.c.status == "active",
                        )
                    )
                ).scalar_one()
        data: dict[str, Any] = {
            "organization": {"id": str(organization_id), "name": organization.name},
            "role": {"key": principal.role_key or "", "name": role.name if role else ""},
            "teams": [{"id": str(team.id), "name": team.name} for team in teams],
        }
        if members is not None:
            data["member_count"] = int(members)
        return ToolOutput(data=data)

    return ToolDefinition(
        id="workspace_info",
        name="Workspace",
        description=(
            "The organization (workspace) the user is working in, their role in it and the "
            "teams they belong to."
        ),
        version="1",
        category="workspace",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {
                "organization": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["id", "name"],
                },
                "role": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["key", "name"],
                },
                "teams": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                        "required": ["id", "name"],
                    },
                },
                "member_count": {"type": "integer"},
            },
            "required": ["organization", "role", "teams"],
            "additionalProperties": False,
        },
        handler=workspace_info,
        permissions=frozenset({"organization.view"}),
        risk="low",
        timeout_seconds=5.0,
        max_retries=1,
        max_input_bytes=64,
        max_output_bytes=8_000,
        describe=lambda arguments: "Checking your workspace",
    )
