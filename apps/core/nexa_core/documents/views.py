"""`/api/v1/documents/*` (docs/api/documents.md).

Every route needs a verified token with an active organization; the service applies the
visibility rules and the `documents.*` permissions. Uploads are multipart; the text and the
original bytes have their own routes with their own content types.
"""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.http import FileResponse, HttpResponse
from rest_framework import serializers
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from nexa_common.errors import FieldError, PermissionDenied, ValidationFailed
from nexa_core.access.drf import AuthenticatedView, current_auth, current_user
from nexa_core.access.principal import OrganizationContext, load_organization_context
from nexa_core.api import ok
from nexa_core.documents import lifecycle, services
from nexa_core.documents.extraction.sniff import EXTENSIONS
from nexa_core.documents.models import Document
from nexa_core.documents.serializers import (
    UpdateInput,
    UploadInput,
    document_payload,
    status_payload,
)

PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
DEFAULT_CONTENT_CHARS = 20_000
_FORMATS = {choice for choice, _ in Document.Format.choices}
_NO_STORE = "private, no-store"


def _validated(
    serializer_class: type[serializers.Serializer[Any]], data: Any, **kwargs: Any
) -> dict[str, Any]:
    serializer = serializer_class(data=data, **kwargs)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _invalid(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(details=[FieldError(field=field, code="invalid", message=message)])


class DocumentsView(AuthenticatedView):
    """The organization comes from the token, as for `/ai/usage/`: these routes are not
    prefixed by an organization id."""

    context: OrganizationContext

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        auth = current_auth(request)
        if auth.claims.organization_id is None:
            raise PermissionDenied("Select an organization first.")
        self.context = load_organization_context(
            current_user(request), auth.claims.organization_id, auth.session.id
        )

    def document(self, document_id: UUID, *, action: str = "view") -> Document:
        return services.get_document(self.context, document_id, action=action)


class DocumentCollectionView(DocumentsView):
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def get(self, request: Request) -> Response:
        params = request.query_params
        documents = services.visible_documents(self.context)
        if status := params.get("status"):
            if status not in lifecycle.STATUSES:
                raise _invalid("status", f"Use one of {', '.join(lifecycle.STATUSES)}.")
            documents = documents.filter(status=status)
        if fmt := params.get("format"):
            if fmt not in _FORMATS:
                raise _invalid("format", f"Use one of {', '.join(sorted(_FORMATS))}.")
            documents = documents.filter(format=fmt)
        if owner := params.get("owner"):
            if owner == "me":
                documents = documents.filter(uploaded_by_id=self.context.principal.user_id)
            else:
                documents = documents.filter(uploaded_by_id=_uuid("owner", owner))
        if category := params.get("category"):
            documents = documents.filter(category=category[:32])
        if team := params.get("team_id"):
            documents = documents.filter(team_id=_uuid("team_id", team))
        if conversation := params.get("conversation_id"):
            documents = documents.filter(conversation_id=_uuid("conversation_id", conversation))
        if query := params.get("q", "").strip():
            if "\x00" in query:
                raise _invalid("q", "Search text cannot contain NUL characters.")
            documents = documents.filter(filename__icontains=query[:200])
        if cursor := params.get("cursor"):
            documents = documents.filter(id__lt=_uuid("cursor", cursor))
        limit = _limit(params.get("limit"), PAGE_SIZE, MAX_PAGE_SIZE)
        # Keyset pagination on the time-ordered UUIDv7 primary key, newest first.
        page = list(documents.order_by("-id")[: limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        return ok(
            [document_payload(d, self.context.principal) for d in page],
            meta={
                "next_cursor": str(page[-1].id) if has_more and page else None,
                "has_more": has_more,
                "limits": upload_limits(),
            },
        )

    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationFailed(
                details=[FieldError(field="file", code="required", message="Attach a file.")]
            )
        data = _validated(UploadInput, request.data)
        document = services.store_upload(
            self.context,
            upload,
            services.UploadFields(
                title=data["title"],
                category=data["category"],
                visibility=data["visibility"],
                team_id=data["team_id"],
                conversation_id=data["conversation_id"],
            ),
        )
        document = services.get_document(self.context, document.id)
        return ok(
            document_payload(document, self.context.principal),
            message="Upload accepted. Processing has started.",
            status=201,
        )


class DocumentView(DocumentsView):
    def get(self, request: Request, document_id: UUID) -> Response:
        document = self.document(document_id)
        ingestions = list(document.ingestions.order_by("-attempt")[:10])
        return ok(document_payload(document, self.context.principal, ingestions=ingestions))

    def patch(self, request: Request, document_id: UUID) -> Response:
        document = self.document(document_id, action="update")
        changes = _validated(UpdateInput, request.data, partial=True)
        document = services.update_document(self.context, document, changes)
        document = self.document(document_id)
        return ok(document_payload(document, self.context.principal), message="Document updated.")

    def delete(self, request: Request, document_id: UUID) -> Response:
        document = self.document(document_id, action="delete")
        services.delete_document(self.context, document)
        return ok(None, message="Document deleted.")


class DocumentStatusView(DocumentsView):
    def get(self, request: Request, document_id: UUID) -> Response:
        return ok(status_payload(self.document(document_id)))


class DocumentContentView(DocumentsView):
    def get(self, request: Request, document_id: UUID) -> Response:
        document = self.document(document_id)
        params = request.query_params
        if page := params.get("page"):
            try:
                number = int(page)
            except ValueError:
                raise _invalid("page", "Use a page number.") from None
            return ok(services.page_content(document, number))
        offset = _limit(params.get("offset"), 0, 10**12, minimum=0)
        limit = _limit(params.get("limit"), DEFAULT_CONTENT_CHARS, services.MAX_CONTENT_CHARS)
        return ok(services.content_slice(document, offset=offset, limit=limit))


class DocumentPreviewView(DocumentsView):
    """The extracted text as plain text, never as HTML: a browser renders it as text."""

    def get(self, request: Request, document_id: UUID) -> HttpResponse:
        document = self.document(document_id)
        text = services.preview_text(document)
        response = HttpResponse(text, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = _disposition("inline", f"{document.filename}.txt")
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = _NO_STORE
        return response


class DocumentDownloadView(DocumentsView):
    """The original bytes as an attachment, so nothing is rendered in the browser."""

    def get(self, request: Request, document_id: UUID) -> FileResponse:
        document = self.document(document_id)
        services.record_download(self.context, document)
        response = FileResponse(
            services.open_file(document),
            as_attachment=True,
            filename=document.filename,
            content_type=document.media_type,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = _NO_STORE
        return response


class DocumentRetryView(DocumentsView):
    def post(self, request: Request, document_id: UUID) -> Response:
        document = self.document(document_id, action="update")
        services.retry_processing(self.context, document)
        document = self.document(document_id)
        return ok(
            document_payload(document, self.context.principal),
            message="Processing has been queued again.",
            status=202,
        )


def upload_limits() -> dict[str, Any]:
    """What a client may check before sending a file; the server checks again."""
    return {
        "max_upload_bytes": settings.NEXA_DOCUMENTS_MAX_UPLOAD_BYTES,
        "accepted_extensions": sorted(EXTENSIONS),
    }


def _uuid(field: str, value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise _invalid(field, "Use a valid id.") from None


def _limit(raw: str | None, default: int, maximum: int, *, minimum: int = 1) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _disposition(kind: str, filename: str) -> str:
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode() or "document"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
