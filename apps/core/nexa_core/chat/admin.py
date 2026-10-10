"""Read-only views of conversations for platform staff. Runtime is the only writer, and
message content is private: the admin shows metadata, not text."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from nexa_core.chat.models import Conversation, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin[Conversation]):
    list_display = (
        "id",
        "title",
        "organization",
        "owner",
        "model",
        "status",
        "message_count",
        "last_message_at",
    )
    list_filter = ("status", "model")
    search_fields = ("id", "title", "owner__email")
    readonly_fields = tuple(f.name for f in Conversation._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin[Message]):
    list_display = (
        "id",
        "conversation",
        "sequence",
        "role",
        "status",
        "provider_model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "error_code",
        "created_at",
    )
    list_filter = ("role", "status", "provider")
    search_fields = ("id", "conversation__id", "request_id")
    exclude = ("content",)
    readonly_fields = tuple(f.name for f in Message._meta.fields if f.name != "content")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
