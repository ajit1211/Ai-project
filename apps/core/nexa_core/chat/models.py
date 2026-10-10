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
        return self.title or f"Conversation {self.id}"


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

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sequence = models.PositiveIntegerField(help_text="Position in the conversation, from 1.")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COMPLETED)
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
        ]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="messages_org_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.role} #{self.sequence} in {self.conversation_id}"
