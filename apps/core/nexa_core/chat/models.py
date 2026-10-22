"""Conversations and messages (docs/architecture/chat-engine.md).

    Organization ─< Conversation ─< Message
                       │
                       └─ owner (User)

Core owns the schema, the migrations and the admin, as for every table. Runtime is the only
writer: it creates conversations, appends messages and updates generation state over its own
connection (a migration grants it INSERT, UPDATE and DELETE on these two tables), because the
token stream must not pass through Django. A conversation is private to its owner inside one
organization; `messages.organization_id` is denormalized and tied to the conversation by a
composite foreign key, so a message can never point at another tenant's conversation.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from nexa_common.ids import new_id
from nexa_core.organizations.models import Organization

DEFAULT_PROMPT_VERSION = "nexa-assistant/v1"


class Conversation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        ARCHIVED = "archived"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="conversations"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations"
    )
    title = models.CharField(max_length=200, blank=True)
    model = models.CharField(
        max_length=48, blank=True, help_text="Logical model name; empty means the default."
    )
    prompt_version = models.CharField(max_length=64, default=DEFAULT_PROMPT_VERSION)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    message_count = models.PositiveIntegerField(default=0)
    # The assistant message being generated right now, if any. One generation per
    # conversation at a time; a stale lock (a crashed process) expires with the AI timeout.
    active_generation_id = models.UUIDField(null=True, blank=True)
    generation_started_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "conversations"
        ordering = ("-last_message_at", "-id")
        constraints = [
            # Target of the composite foreign key that keeps messages inside one tenant.
            models.UniqueConstraint(
                fields=["organization", "id"], name="conversations_org_id_uniq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "owner", "-last_message_at"],
                name="conversations_owner_recent_idx",
            ),
        ]

    def __str__(self) -> str:
        # Not the title: it is message text, and this string shows up in the admin and logs.
        return f"Conversation {self.id}"


class Message(models.Model):
    class Role(models.TextChoices):
        SYSTEM = "system"
        USER = "user"
        ASSISTANT = "assistant"

    class Status(models.TextChoices):
        PENDING = "pending"
        STREAMING = "streaming"
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"
        # Paused until the user approves or rejects an action a tool would take (Phase 10).
        AWAITING_APPROVAL = "awaiting_approval"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sequence = models.PositiveIntegerField(help_text="Position in the conversation, from 1.")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.COMPLETED)
    # Caller-supplied idempotency key for user messages: a retried submission is not a new one.
    client_message_id = models.CharField(max_length=64, blank=True)
    # Generation details for assistant messages.
    model = models.CharField(max_length=48, blank=True, help_text="Logical model requested.")
    provider = models.CharField(max_length=32, blank=True)
    provider_model = models.CharField(max_length=128, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    ttft_ms = models.PositiveIntegerField(null=True, blank=True)
    usage_event_id = models.UUIDField(
        null=True, blank=True, help_text="The AI usage ledger row (ai_usage_events.id)."
    )
    request_id = models.CharField(max_length=128, blank=True)
    finish_reason = models.CharField(max_length=16, blank=True)
    error_code = models.CharField(max_length=48, blank=True)
    # A reply grounded in the user's documents (Phase 08, docs/architecture/rag.md): the scope
    # that was asked for and how retrieval and citation went. Null for ordinary replies.
    grounding = models.JSONField(null=True, blank=True)
    # A reply that used tools (Phase 10, docs/architecture/tools.md): the text of each model
    # call and the tool calls it made, in order, so a paused turn can resume exactly. The
    # calls themselves are `tool_invocations` rows. Null for replies without tools.
    tool_trace = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "messages"
        ordering = ("conversation", "sequence")
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="messages_sequence_uniq"
            ),
            models.UniqueConstraint(
                fields=["conversation", "client_message_id"],
                condition=~Q(client_message_id=""),
                name="messages_client_id_uniq",
            ),
            models.CheckConstraint(condition=Q(sequence__gte=1), name="messages_sequence_chk"),
            # Target of the composite key that keeps a message's sources in its tenant.
            models.UniqueConstraint(fields=["organization", "id"], name="messages_org_id_uniq"),
        ]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="messages_org_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.role} #{self.sequence} in {self.conversation_id}"


class MessageSource(models.Model):
    """A passage that was given to the model for a grounded reply, and whether the reply
    cited it.

    Only references are stored: the document and the chunk. The excerpt, page and section
    are read from the chunk, and the document re-authorized, every time the source is shown,
    so a deleted or no-longer-shared document stops appearing in old conversations. Runtime
    writes these rows with the reply; deleting the message deletes them.
    """

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="sources")
    document = models.ForeignKey(
        "documents.Document", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Not a foreign key: chunks are replaced when a document is re-indexed, and a citation of
    # text that no longer exists must show as unavailable rather than disappear.
    chunk_id = models.UUIDField()
    label = models.CharField(max_length=8, help_text="The citation label given to the model.")
    rank = models.PositiveSmallIntegerField(help_text="1 for the best-ranked passage.")
    score = models.FloatField(default=0.0)
    cited = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "message_sources"
        ordering = ("message", "rank")
        constraints = [
            models.UniqueConstraint(fields=["message", "label"], name="message_sources_label_uniq"),
            models.CheckConstraint(condition=Q(rank__gte=1), name="message_sources_rank_chk"),
        ]
        indexes = [
            models.Index(fields=["document"], name="message_sources_document_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.label} of {self.message_id}"
