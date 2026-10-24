"""AI agents and their runs (Phase 11, docs/architecture/agents.md).

    Organization ─< Agent ─< AgentRun ─< AgentStep
                              │
                              └─< ToolInvocation (run_id) ─┤ ToolApproval

An **agent** is configuration: instructions, the tools it may use, the model, its limits and
its approval policy. It is not code: handlers come from Runtime's tool registry, and an agent
can only name tool ids. Platform agents (no organization, `is_system`) are seeded by
`nexa_core.agents.catalog` and visible to every organization; an organization's own agents are
created by its members.

A **run** is one task given to an agent by one user: the goal, a snapshot of the agent's
configuration at submission (a later change to the agent never alters a running task), the
lifecycle state, budgets and counters, and the result. **Steps** are the run's append-only
history: each model decision, tool call, approval and the finalization, with sanitized
summaries (never a hidden reasoning trace).

Core owns the schema, the migrations and the admin; Runtime executes runs and writes runs and
steps (a migration grants it those writes). Composite foreign keys keep every row inside its
organization and cascade in the database.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from nexa_common.ids import new_id
from nexa_core.organizations.models import Organization

MAX_NAME_CHARS = 80
MAX_SLUG_CHARS = 64
MAX_DESCRIPTION_CHARS = 500
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_GOAL_CHARS = 10_000
MAX_TOOLS = 32

TOOL_ID = RegexValidator(r"^[a-z][a-z0-9_]{1,63}$", "Lowercase letters, digits and underscores.")
SLUG = RegexValidator(r"^[a-z0-9][a-z0-9-]{0,63}$", "Lowercase letters, digits and dashes.")

APPROVAL_POLICIES = [
    ("registry", "Tools that need approval by their definition"),
    ("all_tool_calls", "Every tool call"),
]


class Agent(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="agents",
        help_text="Empty: a platform agent, available to every organization.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agents",
    )
    name = models.CharField(max_length=MAX_NAME_CHARS)
    slug = models.CharField(max_length=MAX_SLUG_CHARS, validators=[SLUG])
    description = models.CharField(max_length=MAX_DESCRIPTION_CHARS, blank=True)
    instructions = models.TextField(max_length=MAX_INSTRUCTIONS_CHARS)
    # Bumped on every change to the behaviour (instructions, tools, model, limits, policy,
    # schemas); every run records the version it was started with.
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False, help_text="Seeded by NEXA; not editable.")
    model = models.CharField(
        max_length=48, blank=True, help_text="Logical model name; empty means the default."
    )
    allowed_tools = ArrayField(
        models.CharField(max_length=64, validators=[TOOL_ID]),
        default=list,
        blank=True,
        size=MAX_TOOLS,
    )
    input_schema = models.JSONField(null=True, blank=True)
    output_schema = models.JSONField(null=True, blank=True)
    max_steps = models.PositiveSmallIntegerField(default=12)
    max_tool_calls = models.PositiveSmallIntegerField(default=20)
    max_duration_seconds = models.PositiveIntegerField(default=600)
    max_tokens = models.PositiveIntegerField(default=200_000)
    approval_policy = models.CharField(max_length=16, choices=APPROVAL_POLICIES, default="registry")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "agents"
        ordering = ("name", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"],
                condition=Q(organization__isnull=False, deleted_at__isnull=True),
                name="agents_org_slug_uniq",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(organization__isnull=True, deleted_at__isnull=True),
                name="agents_platform_slug_uniq",
            ),
            # Target of the composite key that keeps a run's agent in the run's organization
            # (platform agents have no organization and are referenced by id alone).
            models.UniqueConstraint(fields=["organization", "id"], name="agents_org_id_uniq"),
            models.CheckConstraint(
                condition=Q(
                    max_steps__gte=1,
                    max_tool_calls__gte=0,
                    max_duration_seconds__gte=10,
                    max_tokens__gte=1000,
                ),
                name="agents_limits_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "enabled"], name="agents_org_enabled_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class AgentRun(models.Model):
    """One task. `status` follows the lifecycle in docs/architecture/agents.md#lifecycle;
    transitions are made by Runtime with conditional updates only."""

    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        WAITING_FOR_APPROVAL = "waiting_for_approval"
        PAUSED = "paused"
        CANCEL_REQUESTED = "cancel_requested"
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"
        TIMED_OUT = "timed_out"

    ACTIVE = ("queued", "running", "waiting_for_approval", "paused", "cancel_requested")
    FINAL = ("completed", "failed", "cancelled", "timed_out")

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="runs")
    agent_version = models.PositiveIntegerField()
    # The agent as it was at submission: instructions, tools, model, limits, policy, schemas.
    agent_snapshot = models.JSONField(default=dict)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_runs"
    )
    conversation = models.ForeignKey(
        "chat.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    goal = models.TextField(max_length=MAX_GOAL_CHARS)
    input = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    # Why a run ended early: step_limit, tool_call_limit, token_budget, timeout, tool_error,
    # validation_failed, provider_error, approval_expired, cancelled, interrupted, …
    failure_reason = models.CharField(max_length=48, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    progress_message = models.CharField(max_length=300, blank=True)
    model = models.CharField(max_length=48, blank=True)
    # Counters (measured, from the usage ledger and the step records).
    step_count = models.PositiveIntegerField(default=0)
    model_calls = models.PositiveIntegerField(default=0)
    tool_call_count = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(null=True, blank=True)
    # The result.
    result = models.TextField(blank=True)
    output = models.JSONField(null=True, blank=True)
    # References to the passages the answer rests on (document and chunk ids), re-authorized
    # whenever they are read; never copied text.
    sources = models.JSONField(default=list, blank=True)
    # Cooperative control flags, checked between steps and before any tool runs.
    cancel_requested = models.BooleanField(default=False)
    pause_requested = models.BooleanField(default=False)
    # Worker claim: the process running it and until when its lease is valid.
    claimed_by = models.CharField(max_length=128, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    idempotency_key = models.CharField(max_length=128, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # When the run's time budget ends. The clock stops while the run is paused or waits for
    # an approval: the seconds it had left are kept in `time_left_seconds`, and the deadline is
    # set again from them when it continues.
    deadline_at = models.DateTimeField(null=True, blank=True)
    time_left_seconds = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "agent_runs"
        ordering = ("-created_at", "-id")
        constraints = [
            # Keys are per organization: the same key in another organization is another task.
            models.UniqueConstraint(
                fields=["organization", "user", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="agent_runs_idempotency_uniq",
            ),
            # Target of the composite keys that keep steps and tool calls in the run's tenant.
            models.UniqueConstraint(fields=["organization", "id"], name="agent_runs_org_id_uniq"),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "queued",
                        "running",
                        "waiting_for_approval",
                        "paused",
                        "cancel_requested",
                        "completed",
                        "failed",
                        "cancelled",
                        "timed_out",
                    ]
                ),
                name="agent_runs_status_chk",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "user", "-created_at"], name="agent_runs_owner_idx"
            ),
            models.Index(fields=["status", "created_at"], name="agent_runs_queue_idx"),
            models.Index(fields=["agent", "-created_at"], name="agent_runs_agent_idx"),
        ]

    def __str__(self) -> str:
        return f"Run {self.id} ({self.status})"


class AgentStep(models.Model):
    """One entry of a run's history. `detail` holds sanitized, bounded data: a decision's
    summary and action, a tool call's redacted arguments and outcome, an approval's state,
    the final answer. Hidden reasoning is never stored."""

    class Kind(models.TextChoices):
        DECISION = "decision"
        TOOL = "tool"
        APPROVAL = "approval"
        PROGRESS = "progress"
        FINAL = "final"
        ERROR = "error"

    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REJECTED = "rejected"
        CANCELLED = "cancelled"
        WAITING = "waiting"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    sequence = models.PositiveIntegerField(help_text="Position in the run, from 1.")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUCCEEDED)
    summary = models.CharField(max_length=300, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    invocation = models.ForeignKey(
        "tools.ToolInvocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_steps",
    )
    error_code = models.CharField(max_length=48, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    usage_event_id = models.UUIDField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agent_steps"
        ordering = ("run", "sequence")
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="agent_steps_sequence_uniq"),
            models.CheckConstraint(condition=Q(sequence__gte=1), name="agent_steps_sequence_chk"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} #{self.sequence} of {self.run_id}"
