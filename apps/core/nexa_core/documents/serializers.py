"""Request validation and response shapes of the documents API (docs/api/documents.md).

Responses never carry the storage key, the file's location, or the extracted text (that has
its own endpoint). Metadata is what the parser found, keyed by plain names.
"""

from typing import Any

from django.utils import timezone
from rest_framework import serializers

from nexa_common.access import Principal, can
from nexa_core.documents import lifecycle
from nexa_core.documents.models import (
    MAX_CATEGORY_CHARS,
    MAX_TITLE_CHARS,
    Document,
    DocumentIngestion,
)

VISIBILITIES = ("private", "team", "organization")
CATEGORY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class OptionalUUIDField(serializers.UUIDField):
    """A UUID that a form may leave empty: "" means null, as a form cannot send null."""

    def to_internal_value(self, data: Any) -> Any:
        if data == "":
            return None
        return super().to_internal_value(data)


class UploadInput(serializers.Serializer[dict[str, Any]]):
    """The fields that may accompany the file in a multipart upload."""

    title = serializers.CharField(
        max_length=MAX_TITLE_CHARS, required=False, allow_blank=True, default=""
    )
    category = serializers.RegexField(
        CATEGORY_PATTERN,
        max_length=MAX_CATEGORY_CHARS,
        required=False,
        allow_blank=True,
        default="",
    )
    visibility = serializers.ChoiceField(choices=VISIBILITIES, required=False, default="private")
    team_id = OptionalUUIDField(required=False, allow_null=True, default=None)
    conversation_id = OptionalUUIDField(required=False, allow_null=True, default=None)


class UpdateInput(serializers.Serializer[dict[str, Any]]):
    """Fields a user may change; sent as a partial update, so absent means untouched."""

    title = serializers.CharField(max_length=MAX_TITLE_CHARS, required=False, allow_blank=True)
    category = serializers.RegexField(
        CATEGORY_PATTERN, max_length=MAX_CATEGORY_CHARS, required=False, allow_blank=True
    )
    visibility = serializers.ChoiceField(choices=VISIBILITIES, required=False)
    team_id = OptionalUUIDField(required=False, allow_null=True)
    conversation_id = OptionalUUIDField(required=False, allow_null=True)


def document_payload(
    document: Document,
    principal: Principal,
    *,
    ingestions: list[DocumentIngestion] | None = None,
) -> dict[str, Any]:
    from nexa_core.documents.services import can_retry, document_resource  # no import cycle

    resource = document_resource(document)
    owner = document.uploaded_by
    payload: dict[str, Any] = {
        "id": str(document.id),
        "filename": document.filename,
        "title": document.title,
        "category": document.category,
        "format": document.format,
        "media_type": document.media_type,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "visibility": document.visibility,
        "team_id": str(document.team_id) if document.team_id else None,
        "conversation_id": str(document.conversation_id) if document.conversation_id else None,
        "owner": {"id": str(owner.id), "email": owner.email} if owner is not None else None,
        "status": document.status,
        "error_code": document.error_code or None,
        "error_message": document.error_message or None,
        "attempt_count": document.attempt_count,
        "page_count": document.page_count,
        "word_count": document.word_count,
        "char_count": document.char_count,
        "warnings": list(document.warnings or []),
        "metadata": dict(document.metadata or {}),
        "parser_version": document.parser_version or None,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
        "processing_started_at": _iso(document.processing_started_at),
        "processed_at": _iso(document.processed_at),
        "has_text": document.status == lifecycle.COMPLETED,
        "can_retry": can_retry(document, now=timezone.now()) and can(principal, "update", resource),
        "editable": can(principal, "update", resource),
        "deletable": can(principal, "delete", resource),
    }
    if ingestions is not None:
        payload["ingestions"] = [ingestion_payload(i) for i in ingestions]
    return payload


def ingestion_payload(ingestion: DocumentIngestion) -> dict[str, Any]:
    return {
        "id": str(ingestion.id),
        "attempt": ingestion.attempt,
        "status": ingestion.status,
        "error_code": ingestion.error_code or None,
        "error_message": ingestion.error_message or None,
        "parser_version": ingestion.parser_version or None,
        "page_count": ingestion.page_count,
        "word_count": ingestion.word_count,
        "char_count": ingestion.char_count,
        "warnings": list(ingestion.warnings or []),
        "duration_ms": ingestion.duration_ms,
        "created_at": ingestion.created_at.isoformat(),
        "started_at": _iso(ingestion.started_at),
        "finished_at": _iso(ingestion.finished_at),
    }


def status_payload(document: Document) -> dict[str, Any]:
    """The light shape the library polls while a document is being processed."""
    return {
        "id": str(document.id),
        "status": document.status,
        "error_code": document.error_code or None,
        "error_message": document.error_message or None,
        "attempt_count": document.attempt_count,
        "processing_started_at": _iso(document.processing_started_at),
        "processed_at": _iso(document.processed_at),
        "updated_at": document.updated_at.isoformat(),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
