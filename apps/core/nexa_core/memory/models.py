"""Long-term memory and conversation summaries (docs/architecture/memory-engine.md).

    Organization ─< Memory ─ owner (User)
                      ├─ team (team scope)
                      ├─ conversation (conversation scope, or the source conversation)
                      └─ source_message
    Conversation ─< ConversationSummary
    Message ─ MemoryExtraction (one per answered user message: the idempotency claim)

A conversation is a record of what was said; a memory is one selected piece of information
that may matter in a later interaction. Core owns the schema, migrations and admin, as for
every table. Runtime writes all four tables (a migration grants it that), because memories
are extracted, retrieved and summarized in the generation path.

Every memory belongs to exactly one organization (the tenant wall) and has an owner. Its
scope says who may see it: `conversation` and `user` memories are private to their owner,
even from administrators; `team` memories are visible to the team's members; `organization`
memories to every member. "Project memory" in the Phase 06 brief is team memory here: NEXA
has no project entity, and the team is its unit of shared work (ADR-034).
"""

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models import Q

from nexa_common.ids import new_id
from nexa_core.chat.models import Conversation, Message
from nexa_core.organizations.models import Organization, Team

MAX_CONTENT_CHARS = 2_000
MAX_KEY_CHARS = 80


class Memory(models.Model):
    class Scope(models.TextChoices):
        CONVERSATION = "conversation"
        USER = "user"
        TEAM = "team"
        ORGANIZATION = "organization"

    class Kind(models.TextChoices):
        PREFERENCE = "preference"  # how to answer; recalled on every turn
        FACT = "fact"  # about the user or their work
        DECISION = "decision"  # a choice made; history is kept when it changes
        EPISODE = "episode"  # what an earlier conversation was about

    class SourceType(models.TextChoices):
        EXPLICIT = "explicit"  # the user said so
        INFERRED = "inferred"  # the model concluded it; never active without approval
        ASSISTANT = "assistant"  # the assistant suggested it; never active without approval
        MANUAL = "manual"  # typed in by the user
        SUMMARY = "summary"  # produced by conversation summarization

    class Status(models.TextChoices):
        CANDIDATE = "candidate"
        ACTIVE = "active"
        SUPERSEDED = "superseded"
        EXPIRED = "expired"
        REJECTED = "rejected"

    PRIVATE_SCOPES = (Scope.CONVERSATION, Scope.USER)

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memories"
    )
    # The subject of a private memory; the author of a shared one.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memories"
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, null=True, blank=True, related_name="memories"
    )
    # The conversation it belongs to (conversation scope) or came from (every other scope).
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="memories"
    )
    source_message = models.ForeignKey(
        Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    scope = models.CharField(max_length=16, choices=Scope.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # A normalized subject such as "preferred_language": one active memory per subject and
    # scope target, so a newer statement supersedes the older one instead of sitting beside it.
    key = models.CharField(max_length=MAX_KEY_CHARS, blank=True)
    content = models.TextField(max_length=MAX_CONTENT_CHARS)
    value = models.JSONField(null=True, blank=True, help_text="A structured value, if any.")
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    confidence = models.FloatField(default=1.0)
    importance = models.PositiveSmallIntegerField(default=3)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CANDIDATE)
    superseded_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    content_hash = models.CharField(max_length=64)
    search = models.GeneratedField(
        expression=SearchVector("content", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "memories"
        verbose_name_plural = "memories"
        ordering = ("-created_at", "-id")
        constraints = [
            # A team exactly when the scope is the team. (A conversation-scoped memory loses
            # its conversation when that is deleted; the chat engine deletes such memories
            # with the conversation, and the retention purge removes any left behind.)
            models.CheckConstraint(
                condition=(
                    Q(scope="team", team__isnull=False) | (~Q(scope="team") & Q(team__isnull=True))
                ),
                name="memories_scope_target_chk",
            ),
            models.CheckConstraint(
                condition=Q(confidence__gte=0.0, confidence__lte=1.0),
                name="memories_confidence_chk",
            ),
            models.CheckConstraint(
                condition=Q(importance__gte=1, importance__lte=5), name="memories_importance_chk"
            ),
            # One active memory per subject within each scope target.
            models.UniqueConstraint(
                fields=["organization", "owner", "key"],
                condition=Q(scope="user", status="active") & ~Q(key=""),
                name="memories_user_key_uniq",
            ),
            models.UniqueConstraint(
                fields=["conversation", "owner", "key"],
                condition=Q(scope="conversation", status="active") & ~Q(key=""),
                name="memories_conversation_key_uniq",
            ),
            models.UniqueConstraint(
                fields=["team", "key"],
                condition=Q(scope="team", status="active") & ~Q(key=""),
                name="memories_team_key_uniq",
            ),
            models.UniqueConstraint(
                fields=["organization", "key"],
                condition=Q(scope="organization", status="active") & ~Q(key=""),
                name="memories_organization_key_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "owner", "status", "-created_at"],
                name="memories_owner_idx",
            ),
            models.Index(
                fields=["organization", "scope", "status"], name="memories_scope_status_idx"
            ),
            models.Index(fields=["team", "status"], name="memories_team_idx"),
            models.Index(fields=["conversation"], name="memories_conversation_idx"),
            models.Index(
                fields=["expires_at"],
                condition=Q(expires_at__isnull=False),
                name="memories_expires_idx",
            ),
            GinIndex(fields=["search"], name="memories_search_gin"),
        ]

    def __str__(self) -> str:
        # Never the content: it is private, and this string shows up in the admin and logs.
        return f"{self.scope} {self.kind} {self.id}"


class MemorySettings(models.Model):
    """A member's memory preferences in one organization. Created with defaults on first use."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memory_settings"
    )
    # Extract candidate memories from conversations automatically.
    extraction_enabled = models.BooleanField(default=True)
    # Store what the user stated explicitly without asking first; inferences always wait.
    auto_save = models.BooleanField(default=True)
    # Memories unused for this long expire; null keeps them until deleted.
    retention_days = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "memory_settings"
        verbose_name_plural = "memory settings"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="memory_settings_member_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"Memory settings of {self.user_id} in {self.organization_id}"


class ConversationSummary(models.Model):
    """A rolling summary of a conversation's older messages; the messages themselves stay.

    Each new version covers messages up to `through_sequence` and is built from the previous
    version plus the messages since, so unchanged content is never summarized twice."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="summaries"
    )
    version = models.PositiveIntegerField()
    through_sequence = models.PositiveIntegerField(
        help_text="The last message sequence this summary covers."
    )
    content = models.TextField()
    token_estimate = models.PositiveIntegerField(default=0)
    model = models.CharField(max_length=48, blank=True)
    usage_event_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conversation_summaries"
        verbose_name_plural = "conversation summaries"
        ordering = ("conversation", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "version"], name="conversation_summaries_version_uniq"
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1, through_sequence__gte=1),
                name="conversation_summaries_positive_chk",
            ),
        ]

    def __str__(self) -> str:
        return f"Summary v{self.version} of {self.conversation_id}"


class MemoryExtraction(models.Model):
    """One extraction run per answered user message: the claim that makes the background job
    idempotent, and the record of what it found (counts only, never content)."""

    class Status(models.TextChoices):
        PENDING = "pending"
        COMPLETED = "completed"
        FAILED = "failed"
        SKIPPED = "skipped"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="+")
    message = models.OneToOneField(
        Message, on_delete=models.CASCADE, related_name="memory_extraction"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    candidates_found = models.PositiveSmallIntegerField(default=0)
    stored = models.PositiveSmallIntegerField(default=0)
    rejected = models.PositiveSmallIntegerField(default=0)
    duplicates = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=48, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    usage_event_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "memory_extractions"
        indexes = [
            models.Index(fields=["organization", "-created_at"], name="memory_extractions_org_idx"),
        ]

    def __str__(self) -> str:
        return f"Extraction for message {self.message_id}: {self.status}"
