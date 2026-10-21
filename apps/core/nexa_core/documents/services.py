"""The document service: who may see what, the upload pipeline, retries, changes, deletion
and reading the extracted text (docs/architecture/documents.md).

Everything is scoped by the caller's organization context (the organization from the
verified token, the membership and permissions from the database) and the shared policy:
a document in another organization, or private to someone else, does not exist as far as
the caller can tell.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, cast
from uuid import UUID

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from nexa_common.access import ResourceRef, Visibility, authorize, require
from nexa_common.errors import (
    Conflict,
    FieldError,
    NotFound,
    PayloadTooLarge,
    QuotaExceeded,
    ServiceUnavailable,
    UnsupportedMediaType,
    ValidationFailed,
)
from nexa_common.ids import new_id
from nexa_common.logs import get_logger
from nexa_core.access.principal import OrganizationContext
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.chat.models import Conversation
from nexa_core.documents import indexing, lifecycle, queue
from nexa_core.documents.extraction import (
    HEAD_BYTES,
    MEDIA_TYPES,
    Format,
    format_for_name,
    looks_like,
)
from nexa_core.documents.models import (
    MAX_FILENAME_CHARS,
    Document,
    DocumentIngestion,
    DocumentText,
)
from nexa_core.documents.storage import (
    DocumentStorage,
    StorageError,
    get_storage,
    read_chunks,
    storage_key,
)
from nexa_core.organizations.models import Team
from nexa_core.security import ratelimit

log = get_logger("nexa.core.documents")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PREVIEW_CHARS = 10_000
MAX_CONTENT_CHARS = 100_000


# --- Visibility and authorization ------------------------------------------------------------


def document_resource(document: Document) -> ResourceRef:
    return ResourceRef(
        type="documents",
        id=document.id,
        organization_id=document.organization_id,
        owner_id=document.uploaded_by_id,
        visibility=cast(Visibility, document.visibility),
        team_id=document.team_id,
    )


def visible_documents(context: OrganizationContext) -> QuerySet[Document]:
    """The documents this member may see: their own, their teams', the organization's; or
    all of them with `documents.manage_all`. Deleted documents are never listed."""
    principal = context.principal
    documents = Document.objects.filter(
        organization=context.organization, deleted_at__isnull=True
    ).select_related("uploaded_by", "search_index")
    if principal.has("documents.manage_all"):
        return documents
    allowed = Q(visibility=Document.Visibility.ORGANIZATION) | Q(uploaded_by_id=principal.user_id)
    if principal.team_ids:
        allowed |= Q(visibility=Document.Visibility.TEAM, team_id__in=list(principal.team_ids))
    return documents.filter(allowed)


def get_document(
    context: OrganizationContext, document_id: UUID, *, action: str = "view"
) -> Document:
    """The document, if the caller may `action` it: 404 when it is in another organization,
    deleted, or invisible to them; 403 when visible but beyond their role."""
    document = (
        Document.objects.select_related("uploaded_by", "search_index")
        .filter(organization=context.organization, id=document_id, deleted_at__isnull=True)
        .first()
    )
    if document is None:
        raise NotFound
    authorize(context.principal, action, document_resource(document))
    return document


# --- Upload ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadFields:
    title: str = ""
    category: str = ""
    visibility: str = Document.Visibility.PRIVATE
    team_id: UUID | None = None
    conversation_id: UUID | None = None


@dataclass(slots=True)
class Checked:
    filename: str
    format: Format
    size: int
    sha256: str
    warnings: list[str] = field(default_factory=list)


def sanitize_filename(name: str) -> str:
    """A display name from whatever the client sent: the last path segment, no control
    characters, normalized, at most 255 characters with the extension kept."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL.sub("", name)
    name = " ".join(name.split()).strip(". ")
    suffix = Path(name).suffix.lower()
    stem = name[: -len(suffix)] if suffix else name
    stem = stem.strip(". ") or "document"
    room = MAX_FILENAME_CHARS - len(suffix)
    return stem[:room].rstrip(". ") + suffix


def check_upload(upload: UploadedFile[bytes], *, max_bytes: int) -> Checked:
    """Everything that can be refused before a byte is stored: the extension, the size, the
    content's signature. The client's content type is not consulted."""
    filename = sanitize_filename(upload.name or "")
    fmt = format_for_name(filename)
    if fmt is None:
        raise UnsupportedMediaType(
            "This file type is not supported. Upload a PDF, Word (.docx), text, Markdown or CSV file."
        )
    size = int(upload.size or 0)
    if size <= 0:
        raise ValidationFailed(
            details=[FieldError(field="file", code="empty", message="The file is empty.")]
        )
    if size > max_bytes:
        raise PayloadTooLarge(f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.")
    upload.seek(0)
    head = upload.read(HEAD_BYTES)
    upload.seek(0)
    if not looks_like(fmt, head, file=upload if fmt == "docx" else None):
        raise UnsupportedMediaType(f"The file's content does not look like a {fmt.upper()} file.")
    upload.seek(0)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return Checked(filename=filename, format=fmt, size=size, sha256=digest.hexdigest())


def _upload_limit() -> ratelimit.Limit:
    return ratelimit.Limit(
        "document_uploads_user", settings.NEXA_DOCUMENTS_UPLOADS_PER_HOUR, 60 * 60
    )


def _check_storage_quota(context: OrganizationContext, size: int) -> None:
    used = (
        Document.objects.filter(
            organization=context.organization, deleted_at__isnull=True
        ).aggregate(total=Sum("size_bytes"))["total"]
        or 0
    )
    quota = settings.NEXA_DOCUMENTS_ORG_STORAGE_BYTES
    if used + size > quota:
        raise QuotaExceeded(
            "The organization's document storage is full. Delete documents to make room."
        )


def _scope_targets(
    context: OrganizationContext, fields: UploadFields, *, current: Document | None = None
) -> tuple[Team | None, Conversation | None]:
    """The team and conversation a document may point at: a team the caller belongs to (or
    manages), a conversation of their own. Anything else does not exist for them."""
    principal = context.principal
    team: Team | None = None
    if fields.visibility == Document.Visibility.TEAM:
        if fields.team_id is None:
            raise ValidationFailed(
                details=[
                    FieldError(
                        field="team_id", code="required", message="Choose the team to share with."
                    )
                ]
            )
        if fields.team_id not in principal.team_ids and not principal.has("documents.manage_all"):
            raise NotFound
        team = Team.objects.filter(id=fields.team_id, organization=context.organization).first()
        if team is None:
            raise NotFound
    conversation: Conversation | None = None
    if fields.conversation_id is not None:
        if current is not None and current.conversation_id == fields.conversation_id:
            conversation = current.conversation
        else:
            conversation = Conversation.objects.filter(
                id=fields.conversation_id,
                organization=context.organization,
                owner_id=principal.user_id,
            ).first()
            if conversation is None:
                raise NotFound
    return team, conversation


def store_upload(
    context: OrganizationContext, upload: UploadedFile[bytes], fields: UploadFields
) -> Document:
    """The upload pipeline: permission, rate limit, validation, quota, private storage, the
    record and its first attempt, the audit entry, and processing after the commit."""
    principal = context.principal
    require(principal, "documents.upload")
    ratelimit.consume(_upload_limit(), str(principal.user_id))
    checked = check_upload(upload, max_bytes=settings.NEXA_DOCUMENTS_MAX_UPLOAD_BYTES)
    _check_storage_quota(context, checked.size)
    team, conversation = _scope_targets(context, fields)

    document_id = new_id()
    key = storage_key(context.organization.id, document_id, Path(checked.filename).suffix)
    storage = get_storage()
    try:
        written = storage.save(key, upload.chunks())
    except StorageError as exc:
        log.error("documents.storage.write_failed", error=type(exc).__name__)
        raise ServiceUnavailable("The file could not be stored. Please try again.") from exc
    if written != checked.size:
        _forget(storage, key)
        raise ValidationFailed(
            details=[
                FieldError(field="file", code="incomplete", message="The upload was incomplete.")
            ]
        )

    try:
        with transaction.atomic():
            document = Document.objects.create(
                id=document_id,
                organization=context.organization,
                uploaded_by_id=principal.user_id,
                team=team,
                conversation=conversation,
                visibility=fields.visibility,
                filename=checked.filename,
                title=fields.title.strip(),
                category=fields.category,
                format=checked.format,
                media_type=MEDIA_TYPES[checked.format],
                size_bytes=checked.size,
                sha256=checked.sha256,
                storage_key=key,
                status=lifecycle.UPLOADED,
            )
            ingestion = _new_attempt(document)
            record(
                AuditAction.DOCUMENT_UPLOADED,
                actor_id=principal.user_id,
                organization_id=context.organization.id,
                resource_type="document",
                resource_id=document.id,
                metadata={
                    "format": checked.format,
                    "size_bytes": checked.size,
                    "visibility": fields.visibility,
                },
            )
            transaction.on_commit(lambda: dispatch_processing(document.id, ingestion.id))
    except Exception:
        _forget(storage, key)  # the record failed: leave no orphaned bytes behind
        raise
    log.info(
        "documents.upload.accepted",
        document_id=str(document.id),
        organization_id=str(context.organization.id),
        format=checked.format,
        size_bytes=checked.size,
    )
    return document


def _forget(storage: DocumentStorage, key: str) -> None:
    try:
        storage.delete(key)
    except StorageError:
        log.warning("documents.storage.cleanup_failed")


def _new_attempt(document: Document) -> DocumentIngestion:
    """Queue the next processing attempt of a document (inside the caller's transaction)."""
    now = timezone.now()
    attempt = document.attempt_count + 1
    ingestion = DocumentIngestion.objects.create(
        organization_id=document.organization_id,
        document=document,
        attempt=attempt,
        status=lifecycle.QUEUED,
    )
    lifecycle.transition(document, lifecycle.QUEUED, now=now)
    document.attempt_count = attempt
    document.save(
        update_fields=[
            "status",
            "attempt_count",
            "error_code",
            "error_message",
            "processed_at",
            "updated_at",
        ]
    )
    return ingestion


def dispatch_processing(document_id: UUID, ingestion_id: UUID) -> None:
    """Hand the attempt to the worker, or run it here when the broker is unreachable."""
    from nexa_core.documents.tasks import process_document

    queue.dispatch(
        process_document,
        {"document_id": str(document_id), "ingestion_id": str(ingestion_id)},
    )


# --- Retry, update, delete -------------------------------------------------------------------


def is_stale(document: Document, *, now: Any) -> bool:
    """A queued or processing attempt nobody is working on any more."""
    if document.status not in lifecycle.IN_FLIGHT:
        return False
    since = document.processing_started_at or document.updated_at
    timeout = timedelta(seconds=settings.NEXA_DOCUMENTS_PROCESSING_TIMEOUT_SECONDS)
    return bool(since < now - 2 * timeout)


def can_retry(document: Document, *, now: Any) -> bool:
    return document.status in lifecycle.RETRYABLE or is_stale(document, now=now)


@transaction.atomic
def retry_processing(context: OrganizationContext, document: Document) -> Document:
    authorize(context.principal, "update", document_resource(document))
    now = timezone.now()
    document = Document.objects.select_for_update().get(id=document.id)
    if document.deleted_at is not None:
        raise NotFound
    if not can_retry(document, now=now):
        raise Conflict("Only a failed, unsupported or stalled document can be processed again.")
    if document.attempt_count >= settings.NEXA_DOCUMENTS_MAX_ATTEMPTS:
        raise Conflict(
            f"This document has been tried {document.attempt_count} times; the limit is "
            f"{settings.NEXA_DOCUMENTS_MAX_ATTEMPTS}."
        )
    DocumentIngestion.objects.filter(
        document=document, status__in=list(lifecycle.IN_FLIGHT)
    ).update(status=lifecycle.CANCELLED, finished_at=now)
    ingestion = _new_attempt(document)
    record(
        AuditAction.DOCUMENT_RETRIED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="document",
        resource_id=document.id,
        metadata={"attempt": ingestion.attempt},
    )
    transaction.on_commit(lambda: dispatch_processing(document.id, ingestion.id))
    return document


@transaction.atomic
def update_document(
    context: OrganizationContext, document: Document, changes: dict[str, Any]
) -> Document:
    """Title, category, sharing (visibility and team) and the conversation link."""
    authorize(context.principal, "update", document_resource(document))
    fields = UploadFields(
        title=changes.get("title", document.title),
        category=changes.get("category", document.category),
        visibility=changes.get("visibility", document.visibility),
        team_id=changes.get("team_id", document.team_id)
        if changes.get("visibility", document.visibility) == Document.Visibility.TEAM
        else None,
        conversation_id=changes.get("conversation_id", document.conversation_id),
    )
    team, conversation = _scope_targets(context, fields, current=document)
    changed = sorted(
        key
        for key in ("title", "category", "visibility", "team_id", "conversation_id")
        if key in changes
    )
    document.title = fields.title.strip()
    document.category = fields.category
    document.visibility = fields.visibility
    document.team = team
    document.conversation = conversation
    document.save(
        update_fields=["title", "category", "visibility", "team", "conversation", "updated_at"]
    )
    if changed:
        record(
            AuditAction.DOCUMENT_UPDATED,
            actor_id=context.principal.user_id,
            organization_id=context.organization.id,
            resource_type="document",
            resource_id=document.id,
            metadata={"fields": changed, "visibility": document.visibility},
        )
    return document


def delete_document(context: OrganizationContext, document: Document) -> None:
    """Soft-delete the record and remove the bytes and the text at once. If the bytes cannot
    be removed now, the purge removes them later; the record is already unreachable."""
    authorize(context.principal, "delete", document_resource(document))
    now = timezone.now()
    with transaction.atomic():
        updated = Document.objects.filter(id=document.id, deleted_at__isnull=True).update(
            deleted_at=now, updated_at=now
        )
        if not updated:
            raise NotFound
        DocumentText.objects.filter(document=document).delete()
        indexing.forget_index(document)  # its passages leave search with it
        DocumentIngestion.objects.filter(
            document=document, status__in=list(lifecycle.IN_FLIGHT)
        ).update(status=lifecycle.CANCELLED, finished_at=now)
        record(
            AuditAction.DOCUMENT_DELETED,
            actor_id=context.principal.user_id,
            organization_id=context.organization.id,
            resource_type="document",
            resource_id=document.id,
            metadata={"format": document.format, "size_bytes": document.size_bytes},
        )
    if remove_file(document):
        log.info("documents.deleted", document_id=str(document.id))


def _index_limit() -> ratelimit.Limit:
    return ratelimit.Limit(
        "document_index_requests_user", settings.NEXA_RAG_INDEX_REQUESTS_PER_HOUR, 60 * 60
    )


def request_reindex(context: OrganizationContext, document: Document) -> Document:
    """Rebuild a document's search index (after a chunking upgrade, or a failed run)."""
    authorize(context.principal, "update", document_resource(document))
    ratelimit.consume(_index_limit(), str(context.principal.user_id))
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(id=document.id)
        if locked.deleted_at is not None:
            raise NotFound
        if locked.status != lifecycle.COMPLETED:
            raise Conflict("The document has no extracted text to index yet.")
        index = indexing.request_indexing(locked)
        record(
            AuditAction.DOCUMENT_INDEX_REQUESTED,
            actor_id=context.principal.user_id,
            organization_id=context.organization.id,
            resource_type="document",
            resource_id=document.id,
            metadata={"generation": index.generation},
        )
    return document


def remove_file(document: Document) -> bool:
    """Delete the stored bytes of a deleted document; returns whether they are gone."""
    try:
        get_storage().delete(document.storage_key)
    except StorageError as exc:
        log.warning(
            "documents.storage.delete_failed",
            document_id=str(document.id),
            error=type(exc).__name__,
        )
        return False
    Document.objects.filter(id=document.id).update(file_deleted_at=timezone.now())
    return True


# --- Reading ---------------------------------------------------------------------------------


def text_of(document: Document) -> DocumentText:
    if document.status != lifecycle.COMPLETED:
        raise Conflict("The document has no extracted text yet.")
    text = DocumentText.objects.filter(document=document).first()
    if text is None:
        raise Conflict("The document has no extracted text yet.")
    return text


def content_slice(document: Document, *, offset: int, limit: int) -> dict[str, Any]:
    text = text_of(document)
    total = text.char_count
    offset = max(0, min(offset, total))
    limit = max(1, min(limit, MAX_CONTENT_CHARS))
    chunk = text.content[offset : offset + limit]
    end = offset + len(chunk)
    return {
        "content": chunk,
        "offset": offset,
        "limit": limit,
        "total_chars": total,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "pages": [
            span
            for span in text.pages
            if isinstance(span, dict)
            and span.get("char_end", 0) > offset
            and span.get("char_start", 0) < end
        ],
    }


def page_content(document: Document, page: int) -> dict[str, Any]:
    text = text_of(document)
    span = next((s for s in text.pages if isinstance(s, dict) and s.get("page") == page), None)
    if span is None:
        raise NotFound
    start, end = int(span["char_start"]), int(span["char_end"])
    return {
        "content": text.content[start:end],
        "page": page,
        "offset": start,
        "limit": end - start,
        "total_chars": text.char_count,
        "has_more": False,
        "next_offset": None,
        "pages": [span],
    }


def preview_text(document: Document) -> str:
    text = text_of(document)
    if text.char_count <= PREVIEW_CHARS:
        return text.content
    return (
        text.content[:PREVIEW_CHARS]
        + "\n\n[Preview truncated. Download the document or read it through the content endpoint.]"
    )


def open_file(document: Document) -> IO[bytes]:
    try:
        return get_storage().open(document.storage_key)
    except StorageError as exc:
        log.error("documents.storage.read_failed", document_id=str(document.id))
        raise ServiceUnavailable("The file is not available right now.") from exc


def file_chunks(document: Document) -> Iterator[bytes]:
    handle = open_file(document)
    try:
        yield from read_chunks(handle)
    finally:
        handle.close()


def record_download(context: OrganizationContext, document: Document) -> None:
    record(
        AuditAction.DOCUMENT_DOWNLOADED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="document",
        resource_id=document.id,
        metadata={"format": document.format},
    )
