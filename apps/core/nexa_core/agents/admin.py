"""Agents are editable by platform staff (platform agents included); runs and their steps are
read-only, and their goals, inputs, results, progress and failure text and step details are
not shown: they come from private
tasks. The admin shows what ran, for whom, how it ended and what it used."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from nexa_core.agents.models import Agent, AgentRun, AgentStep


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin[Agent]):
    list_display = ("name", "slug", "organization", "version", "enabled", "is_system", "updated_at")
    list_filter = ("enabled", "is_system")
    search_fields = ("name", "slug", "organization__name")
    readonly_fields = ("version", "created_at", "updated_at")


_RUN_PRIVATE = (
    "goal",
    "input",
    "result",
    "output",
    "agent_snapshot",
    "sources",
    # Model-written text derived from the task (progress, the reason it gave up).
    "progress_message",
    "error_message",
)


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin[AgentRun]):
    list_display = (
        "id",
        "agent",
        "status",
        "failure_reason",
        "user",
        "step_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "created_at",
    )
    list_filter = ("status", "failure_reason")
    search_fields = ("id", "user__email")
    exclude = _RUN_PRIVATE
    readonly_fields = tuple(f.name for f in AgentRun._meta.fields if f.name not in _RUN_PRIVATE)
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(AgentStep)
class AgentStepAdmin(admin.ModelAdmin[AgentStep]):
    list_display = ("run", "sequence", "kind", "status", "error_code", "duration_ms", "created_at")
    list_filter = ("kind", "status")
    exclude = ("detail", "summary")
    readonly_fields = tuple(
        f.name for f in AgentStep._meta.fields if f.name not in ("detail", "summary")
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
