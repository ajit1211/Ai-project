"""Platform staff switch tools off everywhere here; invocations and approvals are read-only.

Arguments and results are not shown: they come from private conversations (a search query,
a memory being saved). The admin shows what ran, for whom, how it ended and how long it took.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.tools.models import ToolApproval, ToolInvocation, ToolSetting


@admin.register(ToolSetting)
class ToolSettingAdmin(admin.ModelAdmin[ToolSetting]):
    list_display = ("tool_id", "enabled", "organization", "updated_by", "updated_at")
    list_filter = ("enabled", "tool_id")
    search_fields = ("tool_id", "organization__name")
    readonly_fields = ("updated_by", "created_at", "updated_at")

    def save_model(self, request: HttpRequest, obj: ToolSetting, form: Any, change: bool) -> None:
        obj.updated_by = request.user  # type: ignore[assignment]
        super().save_model(request, obj, form, change)
        record(
            AuditAction.TOOL_SETTINGS_CHANGED,
            actor_id=request.user.pk,
            organization_id=obj.organization_id,
            resource_type="tool",
            resource_id=obj.tool_id,
            metadata={
                "enabled": obj.enabled,
                "scope": "organization" if obj.organization_id else "platform",
            },
        )


@admin.register(ToolInvocation)
class ToolInvocationAdmin(admin.ModelAdmin[ToolInvocation]):
    list_display = (
        "id",
        "tool_id",
        "tool_version",
        "status",
        "risk",
        "organization",
        "user",
        "error_code",
        "duration_ms",
        "created_at",
    )
    list_filter = ("status", "tool_id", "risk")
    search_fields = ("id", "message__id", "call_id")
    exclude = ("arguments", "result", "sources")
    readonly_fields = tuple(
        f.name
        for f in ToolInvocation._meta.fields
        if f.name not in ("arguments", "result", "sources")
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ToolApproval)
class ToolApprovalAdmin(admin.ModelAdmin[ToolApproval]):
    list_display = ("id", "tool_id", "status", "risk", "requested_by", "expires_at", "decided_at")
    list_filter = ("status", "tool_id")
    exclude = ("details", "summary")
    readonly_fields = tuple(
        f.name for f in ToolApproval._meta.fields if f.name not in ("details", "summary")
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
