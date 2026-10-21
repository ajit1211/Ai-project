"""Read-only views of documents for platform staff. The content of a document is private:
the admin shows metadata, processing state and timings, never the filename, the text or
where the bytes are."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from nexa_core.documents.models import (
    Document,
    DocumentIndex,
    DocumentIngestion,
    DocumentText,
)

_HIDDEN = ("filename", "title", "storage_key", "metadata")


class ReadOnly(admin.ModelAdmin[Any]):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Document)
class DocumentAdmin(ReadOnly):
    list_display = (
        "id",
        "organization",
        "uploaded_by",
        "format",
        "visibility",
        "status",
        "size_bytes",
        "attempt_count",
        "created_at",
        "deleted_at",
    )
    list_filter = ("format", "visibility", "status")
    search_fields = ("id", "uploaded_by__email", "sha256")
    exclude = _HIDDEN
    readonly_fields = tuple(f.name for f in Document._meta.fields if f.name not in _HIDDEN)
    date_hierarchy = "created_at"


@admin.register(DocumentIngestion)
class DocumentIngestionAdmin(ReadOnly):
    list_display = (
        "document",
        "attempt",
        "status",
        "error_code",
        "duration_ms",
        "page_count",
        "char_count",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("document__id",)
    readonly_fields = tuple(f.name for f in DocumentIngestion._meta.fields)
    date_hierarchy = "created_at"


@admin.register(DocumentText)
class DocumentTextAdmin(ReadOnly):
    list_display = ("document", "char_count", "parser_version", "updated_at")
    search_fields = ("document__id",)
    exclude = ("content", "pages")
    readonly_fields = tuple(
        f.name for f in DocumentText._meta.fields if f.name not in ("content", "pages")
    )


@admin.register(DocumentIndex)
class DocumentIndexAdmin(ReadOnly):
    """Indexing state only; the chunks (document text) are not shown."""

    list_display = (
        "document",
        "status",
        "generation",
        "chunk_count",
        "strategy_version",
        "error_code",
        "indexed_at",
    )
    list_filter = ("status", "strategy_version")
    search_fields = ("document__id",)
    readonly_fields = tuple(f.name for f in DocumentIndex._meta.fields)
