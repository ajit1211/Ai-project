"""AI tool calling: what was called, what needed approval, and which tools are switched off
(docs/architecture/tools.md).

    Message ─< ToolInvocation ─┤ ToolApproval (only for tools that need one)
    Organization ─< ToolSetting (and platform-wide rows without an organization)

Tool definitions are code, not data: Runtime's registry maps each tool id to a trusted,
application-owned handler, and nothing here can add a tool or change what one does. These
tables record what happened and hold the switches that turn a defined tool off.

Core owns the schema, the migrations and the admin; Runtime writes invocations and approvals
while it runs a turn, and organization settings when an administrator changes them through
its API (a migration grants it those writes). Composite foreign keys keep every row inside its
organization, and cascade in the database, because Runtime deletes messages with plain SQL.
"""

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from nexa_common.ids import new_id
from nexa_core.organizations.models import Organization

TOOL_ID = RegexValidator(r"^[a-z][a-z0-9_]{1,63}$", "Lowercase letters, digits and underscores.")
RISKS = [("low", "low"), ("medium", "medium"), ("high", "high")]


class ToolSetting(models.Model):
    """A tool switched on or off: everywhere (no organization; platform staff, in the admin)
    or in one organization (its administrators, `ai.tool.manage`). A tool without a row is on
    when its definition is; a platform-wide "off" wins over any organization's "on"."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tool_settings",
        help_text="Empty: the setting applies to every organization.",
    )
    tool_id = models.CharField(max_length=64, validators=[TOOL_ID])
    enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tool_settings"
        ordering = ("tool_id",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "tool_id"],
                condition=Q(organization__isnull=False),
                name="tool_settings_org_tool_uniq",
            ),
            models.UniqueConstraint(
                fields=["tool_id"],
                condition=Q(organization__isnull=True),
                name="tool_settings_platform_tool_uniq",
            ),
        ]

    def __str__(self) -> str:
        where = "everywhere" if self.organization_id is None else f"in {self.organization_id}"
        return f"{self.tool_id} {'on' if self.enabled else 'off'} {where}"


class ToolInvocation(models.Model):
    """One tool call a model asked for while answering a message, and what became of it.

    `arguments` are the validated arguments with sensitive values redacted; `result` the
    validated, size-bounded output (large document text is never copied here: document tools
    return excerpts and references). `idempotency_key` is unique, so the same call of the same
    turn is never recorded, or run, twice."""

    class Status(models.TextChoices):
        REQUESTED = "requested"
        VALIDATED = "validated"
        AWAITING_APPROVAL = "awaiting_approval"
        APPROVED = "approved"
        EXECUTING = "executing"
        SUCCEEDED = "succeeded"
        REJECTED = "rejected"
        FAILED = "failed"
        TIMED_OUT = "timed_out"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(
        "chat.Conversation", on_delete=models.CASCADE, related_name="tool_invocations"
    )
    message = models.ForeignKey(
        "chat.Message", on_delete=models.CASCADE, related_name="tool_invocations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tool_invocations"
    )
    step = models.PositiveSmallIntegerField(help_text="The model call of the turn, from 0.")
    position = models.PositiveSmallIntegerField(help_text="Order among that call's tool calls.")
    call_id = models.CharField(max_length=128, help_text="The provider's tool-call id.")
    tool_id = models.CharField(max_length=64)
    tool_version = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.REQUESTED)
    risk = models.CharField(max_length=8, choices=RISKS, blank=True)
    arguments = models.JSONField(default=dict, blank=True)
    arguments_hash = models.CharField(max_length=64, blank=True)
    result = models.JSONField(null=True, blank=True)
    result_truncated = models.BooleanField(default=False)
    error_code = models.CharField(max_length=48, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    # References only (document and chunk ids), re-authorized whenever they are shown.
    sources = models.JSONField(default=list, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "tool_invocations"
        ordering = ("message", "step", "position")
        constraints = [
            models.UniqueConstraint(
                fields=["message", "step", "position"], name="tool_invocations_order_uniq"
            ),
            # Target of the composite key that keeps an approval in its tenant.
            models.UniqueConstraint(
                fields=["organization", "id"], name="tool_invocations_org_id_uniq"
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "requested",
                        "validated",
                        "awaiting_approval",
                        "approved",
                        "executing",
                        "succeeded",
                        "rejected",
                        "failed",
                        "timed_out",
                        "cancelled",
                    ]
                ),
                name="tool_invocations_status_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "created_at"], name="tool_invocations_org_idx"),
            models.Index(fields=["tool_id", "status"], name="tool_invocations_tool_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.tool_id} {self.status} ({self.id})"


class ToolApproval(models.Model):
    """A person's decision on one proposed action, bound to that action's exact arguments
    (`arguments_hash`). It expires, it is only ever the requesting user's to make, and it
    grants nothing beyond what that user may do: permissions are checked again when the
    action runs."""

    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        EXPIRED = "expired"
        INVALIDATED = "invalidated"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    invocation = models.OneToOneField(
        ToolInvocation, on_delete=models.CASCADE, related_name="approval"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    tool_id = models.CharField(max_length=64)
    tool_version = models.CharField(max_length=16, blank=True)
    summary = models.CharField(max_length=500)
    # What the person is shown: the action's target and consequences, never secrets.
    details = models.JSONField(default=dict, blank=True)
    arguments_hash = models.CharField(max_length=64)
    risk = models.CharField(max_length=8, choices=RISKS)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tool_approvals"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=["pending", "approved", "rejected", "expired", "invalidated"]
                ),
                name="tool_approvals_status_chk",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "expires_at"], name="tool_approvals_open_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tool_id} approval {self.status} ({self.id})"
