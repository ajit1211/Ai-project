"""Request validation and response shapes of the agents API (docs/api/agents.md#agents)."""

from typing import Any

from rest_framework import serializers

from nexa_common.tool_catalog import TOOLS_BY_ID
from nexa_core.access.principal import OrganizationContext
from nexa_core.agents.models import (
    APPROVAL_POLICIES,
    MAX_DESCRIPTION_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_NAME_CHARS,
    MAX_TOOLS,
    Agent,
)
from nexa_core.agents.services import (
    MAX_DURATION_SECONDS,
    MAX_STEPS,
    MAX_TOKENS,
    MAX_TOOL_CALLS,
    can_edit,
)

MODEL_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"
TOOL_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"


class AgentInput(serializers.Serializer[dict[str, Any]]):
    """Fields of an agent a member may set. On update, absent means untouched."""

    name = serializers.CharField(max_length=MAX_NAME_CHARS, trim_whitespace=True)
    description = serializers.CharField(
        max_length=MAX_DESCRIPTION_CHARS, required=False, allow_blank=True, default=""
    )
    instructions = serializers.CharField(max_length=MAX_INSTRUCTIONS_CHARS)
    model = serializers.RegexField(
        MODEL_PATTERN, required=False, allow_blank=True, default="", max_length=48
    )
    allowed_tools = serializers.ListField(
        child=serializers.RegexField(TOOL_PATTERN, max_length=64),
        max_length=MAX_TOOLS,
        required=False,
        default=list,
    )
    input_schema = serializers.JSONField(required=False, allow_null=True, default=None)
    output_schema = serializers.JSONField(required=False, allow_null=True, default=None)
    max_steps = serializers.IntegerField(
        min_value=1, max_value=MAX_STEPS, required=False, default=12
    )
    max_tool_calls = serializers.IntegerField(
        min_value=0, max_value=MAX_TOOL_CALLS, required=False, default=20
    )
    max_duration_seconds = serializers.IntegerField(
        min_value=10, max_value=MAX_DURATION_SECONDS, required=False, default=600
    )
    max_tokens = serializers.IntegerField(
        min_value=1000, max_value=MAX_TOKENS, required=False, default=200_000
    )
    approval_policy = serializers.ChoiceField(
        choices=[value for value, _ in APPROVAL_POLICIES], required=False, default="registry"
    )
    enabled = serializers.BooleanField(required=False, default=True)

    def validate_name(self, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise serializers.ValidationError("A name is required.")
        return value.strip()

    def validate_instructions(self, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise serializers.ValidationError("Instructions are required.")
        return value

    def _object_schema(self, value: Any) -> Any:
        if value is not None and not isinstance(value, dict):
            raise serializers.ValidationError("A JSON Schema object is required.")
        return value

    def validate_input_schema(self, value: Any) -> Any:
        return self._object_schema(value)

    def validate_output_schema(self, value: Any) -> Any:
        return self._object_schema(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # An agent is configuration only: a field this API does not define (a handler, code, a
        # query) is refused rather than silently dropped.
        data = self.initial_data if isinstance(self.initial_data, dict) else {}
        unknown = sorted(str(key) for key in data if key not in self.fields)
        if unknown:
            raise serializers.ValidationError({unknown[0][:64]: "Unknown field."})
        return attrs


def agent_payload(
    agent: Agent, context: OrganizationContext, *, detail: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(agent.id),
        "name": agent.name,
        "slug": agent.slug,
        "description": agent.description,
        "version": agent.version,
        "enabled": agent.enabled,
        "is_system": agent.is_system,
        "scope": "platform" if agent.organization_id is None else "organization",
        "model": agent.model,
        "tools": [
            {
                "id": tool,
                "name": TOOLS_BY_ID[tool].name if tool in TOOLS_BY_ID else tool,
                "risk": TOOLS_BY_ID[tool].risk if tool in TOOLS_BY_ID else "",
                "requires_approval": TOOLS_BY_ID[tool].requires_approval
                if tool in TOOLS_BY_ID
                else False,
            }
            for tool in agent.allowed_tools
        ],
        "limits": {
            "max_steps": agent.max_steps,
            "max_tool_calls": agent.max_tool_calls,
            "max_duration_seconds": agent.max_duration_seconds,
            "max_tokens": agent.max_tokens,
        },
        "approval_policy": agent.approval_policy,
        "has_input_schema": agent.input_schema is not None,
        "has_output_schema": agent.output_schema is not None,
        "created_by": str(agent.created_by_id) if agent.created_by_id else None,
        "can_edit": can_edit(context, agent),
        "can_delete": can_edit(context, agent, "delete"),
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }
    if detail:
        payload["instructions"] = agent.instructions
        payload["input_schema"] = agent.input_schema
        payload["output_schema"] = agent.output_schema
    return payload
