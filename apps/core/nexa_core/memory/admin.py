"""Read-only views of the memory tables for platform staff. Runtime is the writer, and a
memory's content is private: the admin shows scope, kind, status and dates, never text."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from nexa_core.memory.models import ConversationSummary, Memory, MemoryExtraction, MemorySettings


class ReadOnly(admin.ModelAdmin[Any]):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Memory)
class MemoryAdmin(ReadOnly):
    list_display = (
        "id",
        "organization",
        "owner",
        "scope",
        "kind",
        "source_type",
        "status",
        "confidence",
        "importance",
        "created_at",
        "expires_at",
    )
    list_filter = ("scope", "kind", "source_type", "status")
    search_fields = ("id", "owner__email", "conversation__id")
    exclude = ("content", "value", "key", "search", "content_hash")
    readonly_fields = tuple(
        f.name
        for f in Memory._meta.fields
        if f.name not in ("content", "value", "key", "search", "content_hash")
    )
    date_hierarchy = "created_at"


@admin.register(MemorySettings)
class MemorySettingsAdmin(ReadOnly):
    list_display = ("user", "organization", "extraction_enabled", "auto_save", "retention_days")
    search_fields = ("user__email",)
    readonly_fields = tuple(f.name for f in MemorySettings._meta.fields)


@admin.register(ConversationSummary)
class ConversationSummaryAdmin(ReadOnly):
    list_display = (
        "conversation",
        "version",
        "through_sequence",
        "token_estimate",
        "model",
        "created_at",
    )
    search_fields = ("conversation__id",)
    exclude = ("content",)
    readonly_fields = tuple(f.name for f in ConversationSummary._meta.fields if f.name != "content")


@admin.register(MemoryExtraction)
class MemoryExtractionAdmin(ReadOnly):
    list_display = (
        "message",
        "status",
        "candidates_found",
        "stored",
        "rejected",
        "duplicates",
        "duration_ms",
        "error_code",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("message__id", "conversation__id")
    readonly_fields = tuple(f.name for f in MemoryExtraction._meta.fields)
    date_hierarchy = "created_at"
