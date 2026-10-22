"""Agent definitions (docs/api/agents.md#agents): listing, creating, changing and deleting.

Visibility: platform agents (seeded, no organization) are visible to every member with
`agents.view`; an organization's agents are visible to its members with `agents.view`.
Changing or deleting needs `agents.update` / `agents.delete` and being the agent's creator, or
`agents.manage_all`; platform agents cannot be changed through the API.

A configuration is checked when it is saved: the model is an active logical model available
to the organization, every tool exists, is not switched off, and needs no permission the
author lacks (an agent never carries more authority than its author has, and runs re-check
everything against the user running them), schemas are valid JSON Schemas of objects, and the
limits stay within the platform's maxima. Every change to behaviour bumps `version`; runs keep
the snapshot they started with.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from nexa_ai.errors import AIInvalidRequest
from nexa_ai.structured import check_schema
from nexa_common.access import require
from nexa_common.errors import ErrorCode, FieldError, NexaError, NotFound, PermissionDenied
from nexa_common.tool_catalog import EXECUTE_PERMISSION, TOOLS_BY_ID
from nexa_core.access.principal import OrganizationContext
from nexa_core.agents.models import Agent
from nexa_core.ai.models import AIModelAlias
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.tools.models import ToolSetting

MAX_STEPS = 50
MAX_TOOL_CALLS = 100
MAX_DURATION_SECONDS = 3600
MAX_TOKENS = 1_000_000
BEHAVIOUR_FIELDS = (
    "instructions",
    "model",
    "allowed_tools",
    "input_schema",
    "output_schema",
    "max_steps",
    "max_tool_calls",
    "max_duration_seconds",
    "max_tokens",
    "approval_policy",
)


class AgentInvalid(NexaError):
    status = 422
    code = ErrorCode.VALIDATION_FAILED


class ToolNotAllowed(NexaError):
    status = 422
    code = ErrorCode.TOOL_NOT_ALLOWED


def visible_agents(context: OrganizationContext) -> QuerySet[Agent]:
    require(context.principal, "agents.view")
    return Agent.objects.filter(
        Q(organization=context.organization) | Q(organization__isnull=True),
        deleted_at__isnull=True,
    ).order_by("-is_system", "name", "id")


def get_agent(context: OrganizationContext, agent_id: UUID) -> Agent:
    agent = visible_agents(context).filter(id=agent_id).first()
    if agent is None:
        raise NotFound
    return agent


def can_edit(context: OrganizationContext, agent: Agent, action: str = "update") -> bool:
    principal = context.principal
    if agent.is_system or agent.organization_id != context.organization.id:
        return False
    if not principal.has(f"agents.{action}"):
        return False
    return agent.created_by_id == principal.user_id or principal.has("agents.manage_all")


@transaction.atomic
def create_agent(context: OrganizationContext, data: dict[str, Any]) -> Agent:
    require(context.principal, "agents.create")
    values = _validated(context, data)
    base = _slugify(values["name"])
    for attempt in range(1, 50):
        slug = base if attempt == 1 else f"{base[:58]}-{attempt}"
        try:
            with transaction.atomic():
                agent = Agent.objects.create(
                    organization=context.organization,
                    created_by_id=context.principal.user_id,
                    slug=slug,
                    **values,
                )
            break
        except IntegrityError:
            continue
    else:  # pragma: no cover - fifty agents with the same name
        raise AgentInvalid("Choose a different name.")
    record(
        AuditAction.AGENT_CREATED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="agent",
        resource_id=agent.id,
        metadata={"tools": sorted(agent.allowed_tools), "version": agent.version},
    )
    return agent


@transaction.atomic
def update_agent(context: OrganizationContext, agent_id: UUID, data: dict[str, Any]) -> Agent:
    agent = Agent.objects.select_for_update().filter(id=agent_id).first()
    if agent is None or agent.deleted_at is not None:
        raise NotFound
    visible = agent.organization_id in (None, context.organization.id)
    if not visible:
        raise NotFound
    if not can_edit(context, agent):
        raise PermissionDenied("You cannot change this agent.")
    values = _validated(context, data)
    changed = [name for name, value in values.items() if getattr(agent, name) != value]
    if not changed:
        return agent
    for name in changed:
        setattr(agent, name, values[name])
    if any(name in BEHAVIOUR_FIELDS for name in changed):
        agent.version += 1
    agent.save()
    record(
        AuditAction.AGENT_UPDATED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="agent",
        resource_id=agent.id,
        metadata={"fields": sorted(changed), "version": agent.version},
    )
    return agent


@transaction.atomic
def delete_agent(context: OrganizationContext, agent_id: UUID) -> None:
    agent = Agent.objects.select_for_update().filter(id=agent_id).first()
    if agent is None or agent.deleted_at is not None:
        raise NotFound
    if agent.organization_id not in (None, context.organization.id):
        raise NotFound
    if not can_edit(context, agent, "delete"):
        raise PermissionDenied("You cannot delete this agent.")
    agent.deleted_at = timezone.now()
    agent.enabled = False
    agent.save(update_fields=["deleted_at", "enabled", "updated_at"])
    record(
        AuditAction.AGENT_DELETED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="agent",
        resource_id=agent.id,
        metadata={"version": agent.version},
    )


def _validated(context: OrganizationContext, data: dict[str, Any]) -> dict[str, Any]:
    """The fields to save (all of them on create, the ones sent on update), checked against
    the organization and the author; raises AgentInvalid or ToolNotAllowed."""
    problems: list[FieldError] = []
    values = dict(data)

    if values.get("model"):
        available = (
            AIModelAlias.objects.filter(name=values["model"], is_active=True)
            .filter(Q(available_by_default=True) | Q(grants__organization=context.organization))
            .exists()
        )
        if not available:
            problems.append(_problem("model", "unknown", "That model is not available."))
    if "allowed_tools" in values:
        tools = list(dict.fromkeys(values["allowed_tools"]))
        values["allowed_tools"] = tools
        _check_tools(context, tools)
    for name in ("input_schema", "output_schema"):
        schema = values.get(name)
        if schema:
            try:
                check_schema(schema)
            except AIInvalidRequest as exc:
                problems.append(
                    _problem(
                        name, "invalid", exc.message.replace("The response schema", "The schema")
                    )
                )
    caps = {
        "max_steps": MAX_STEPS,
        "max_tool_calls": MAX_TOOL_CALLS,
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_tokens": MAX_TOKENS,
    }
    for name, cap in caps.items():
        if name in values and values[name] > cap:
            problems.append(_problem(name, "too_large", f"At most {cap}."))
    if problems:
        raise AgentInvalid(details=problems)
    return values


def _check_tools(context: OrganizationContext, tools: list[str]) -> None:
    principal = context.principal
    unknown = [tool for tool in tools if tool not in TOOLS_BY_ID]
    if unknown:
        raise AgentInvalid(
            details=[_problem("allowed_tools", "unknown", f"Unknown tool '{unknown[0][:64]}'.")]
        )
    if tools and not principal.has(EXECUTE_PERMISSION):
        raise ToolNotAllowed("You cannot let AI use tools, so you cannot give an agent tools.")
    switched_off = set(
        ToolSetting.objects.filter(
            Q(organization__isnull=True) | Q(organization=context.organization),
            tool_id__in=tools,
            enabled=False,
        ).values_list("tool_id", flat=True)
    )
    for tool in tools:
        info = TOOLS_BY_ID[tool]
        if tool in switched_off:
            raise ToolNotAllowed(f"The tool '{info.name}' is switched off.")
        missing = sorted(p for p in info.permissions if not principal.has(p))
        if missing:
            raise ToolNotAllowed(
                f"You cannot give an agent '{info.name}': it needs {', '.join(missing)}."
            )


def _problem(field: str, code: str, message: str) -> FieldError:
    return FieldError(field=field, code=code, message=message)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "agent"
