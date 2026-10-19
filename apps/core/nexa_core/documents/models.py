"""Documents: files a member uploads, their processing attempts and their extracted text
(docs/architecture/documents.md).

    Organization ─< Document ─ uploaded_by (User)
                       ├─ team           (team visibility)
                       ├─ conversation   (an optional association)
                       ├─< DocumentIngestion (one row per processing attempt)
                       └─ DocumentText        (one row, once processed)

Core owns everything here and is its only writer: the API and the Celery worker are both
Django, so no other database role needs a grant. The bytes are not in the database;
`storage_key` names them in the private document storage (storage.py). A document is
private to its uploader unless shared with a team or the whole organization, which is an
explicit act. Deletion is soft: the row keeps its metadata until the purge removes it, while
the bytes and the extracted text go at once.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from nexa_common.ids import new_id
from nexa_core.chat.models import Conversation
from nexa_core.documents import lifecycle
from nexa_core.organizations.models import Organization, Team

MAX_FILENAME_CHARS = 255
MAX_TITLE_CHARS = 200
MAX_CATEGORY_CHARS = 32
MAX_ERROR_MESSAGE_CHARS = 300


class Document(models.Model):
    class Status(models.TextChoices):
        UPLOADED = lifecycle.UPLOADED
        QUEUED = lifecycle.QUEUED
        PROCESSING = lifecycle.PROCESSING
        COMPLETED = lifecycle.COMPLETED
        FAILED = lifecycle.FAILED
        NEEDS_OCR = lifecycle.NEEDS_OCR
        UNSUPPORTED = lifecycle.UNSUPPORTED

    class Visibility(models.TextChoices):
        PRIVATE = "private"
        TEAM = "team"
        ORGANIZATION = "organization"

    class Format(models.TextChoices):
        PDF = "pdf"
        DOCX = "docx"
        TXT = "txt"
        MD = "md"
        CSV = "csv"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="documents"
    )
    # The owner. Kept null when the account goes, so shared documents outlive their uploader.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents"
    )
    visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    # The name the user gave the file, sanitized for display and download; never a path.
    filename = models.CharField(max_length=MAX_FILENAME_CHARS)
    title = models.CharField(max_length=MAX_TITLE_CHARS, blank=True)
    category = models.CharField(max_length=MAX_CATEGORY_CHARS, blank=True)
    format = models.CharField(max_length=16, choices=Format.choices)
    media_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    # Server-made, never shown through the API: `<organization>/<document><ext>`.
    storage_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    error_code = models.CharField(max_length=48, blank=True)
    error_message = models.CharField(max_length=MAX_ERROR_MESSAGE_CHARS, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    parser_version = models.CharField(max_length=32, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    char_count = models.PositiveIntegerField(default=0)
    # What the parser found in the file (title, author, columns...), never invented.
    metadata = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # When the stored bytes were removed; a deleted document whose file could not be
    # removed keeps this null until the purge manages it.
    file_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "documents"
        ordering = ("-id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=lifecycle.STATUSES), name="documents_status_chk"
            ),
            models.CheckConstraint(
                condition=Q(visibility__in=["private", "team", "organization"]),
                name="documents_visibility_chk",
            ),
            # A team exactly when the document is shared with one.
            models.CheckConstraint(
                condition=(
                    Q(visibility="team", team__isnull=False)
                    | (~Q(visibility="team") & Q(team__isnull=True))
                ),
                name="documents_team_visibility_chk",
            ),
            models.CheckConstraint(condition=Q(size_bytes__gte=0), name="documents_size_chk"),
            # Target of the composite keys that keep attempts inside one tenant.
            models.UniqueConstraint(fields=["organization", "id"], name="documents_org_id_uniq"),
        ]
        indexes = [
            models.Index(
                fields=["organization", "-id"],
                condition=Q(deleted_at__isnull=True),
                name="documents_org_recent_idx",
            ),
            models.Index(fields=["organization", "uploaded_by"], name="documents_owner_idx"),
            models.Index(fields=["organization", "status"], name="documents_status_idx"),
            models.Index(fields=["organization", "sha256"], name="documents_sha256_idx"),
            models.Index(fields=["team"], name="documents_team_idx"),
            models.Index(fields=["conversation"], name="documents_conversation_idx"),
            models.Index(
                fields=["deleted_at"],
                condition=Q(deleted_at__isnull=False),
                name="documents_deleted_idx",
            ),
        ]

    def __str__(self) -> str:
        # Not the filename: it is user data, and this string shows up in the admin and logs.
        return f"{self.format} document {self.id}"


class DocumentIngestion(models.Model):
    """One attempt to process a document: what it did, how long it took, how it ended."""

    class Status(models.TextChoices):
        QUEUED = lifecycle.QUEUED
        PROCESSING = lifecycle.PROCESSING
        COMPLETED = lifecycle.COMPLETED
        FAILED = lifecycle.FAILED
        NEEDS_OCR = lifecycle.NEEDS_OCR
        UNSUPPORTED = lifecycle.UNSUPPORTED
        CANCELLED = lifecycle.CANCELLED

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="ingestions")
    attempt = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    error_code = models.CharField(max_length=48, blank=True)
    error_message = models.CharField(max_length=MAX_ERROR_MESSAGE_CHARS, blank=True)
    parser_version = models.CharField(max_length=32, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    char_count = models.PositiveIntegerField(default=0)
    warnings = models.JSONField(default=list, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "document_ingestions"
        ordering = ("document", "-attempt")
        constraints = [
            models.UniqueConstraint(
                fields=["document", "attempt"], name="document_ingestions_attempt_uniq"
            ),
            models.CheckConstraint(
                condition=Q(attempt__gte=1), name="document_ingestions_attempt_chk"
            ),
            models.CheckConstraint(
                condition=Q(status__in=lifecycle.ATTEMPT_STATUSES),
                name="document_ingestions_status_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="document_ingestions_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Attempt {self.attempt} for {self.document_id}: {self.status}"


class DocumentText(models.Model):
    """The extracted text, apart from the document so lists never carry it."""

    document = models.OneToOneField(
        Document, on_delete=models.CASCADE, primary_key=True, related_name="text"
    )
    content = models.TextField()
    # [{"page": 1, "char_start": 0, "char_end": 1234}, ...]: where each page sits in content.
    pages = models.JSONField(default=list, blank=True)
    char_count = models.PositiveIntegerField(default=0)
    parser_version = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "document_texts"

    def __str__(self) -> str:
        return f"Text of {self.document_id} ({self.char_count} characters)"
