"""The AI catalog, organization AI policy, and the usage ledger (docs/architecture/ai-gateway.md).

Core owns these tables and their migrations. Runtime reads the catalog and policies, and
inserts into the ledger and nothing else (a migration grants it INSERT on that one table).

    AIModel ─< AIModelPrice                 a concrete provider model and its price versions
    AIModelAlias ─< AIAliasTarget >─ AIModel a logical model and its ordered candidates
    Organization ─ AIOrganizationPolicy      AI switch and limits (platform-owned)
    Organization ─< AIOrganizationAliasGrant access to aliases that are not on by default
    AIUsageEvent                             one row per gateway call, append-only

Provider API keys are never stored here; they come from Runtime's environment.
"""

from typing import Any

from django.contrib.postgres.fields import ArrayField
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from nexa_common.ids import new_id
from nexa_core.organizations.models import Organization


class Provider(models.TextChoices):
    ANTHROPIC = "anthropic", "Anthropic"
    OPENAI = "openai", "OpenAI"
    MOCK = "mock", "Mock (development and tests only)"


class Capability(models.TextChoices):
    """Mirrors `nexa_ai.types.Capability`; Phase 04 exercises the first three."""

    CHAT = "chat"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    REASONING = "reasoning"
    AUDIO = "audio"


alias_name_validator = RegexValidator(
    r"^nexa-[a-z0-9-]{2,40}$", "Logical model names look like 'nexa-fast'."
)


class AIModel(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_model = models.CharField(
        max_length=128, help_text="The provider's own model ID, e.g. claude-sonnet-5-5."
    )
    display_name = models.CharField(max_length=120)
    capabilities = ArrayField(
        models.CharField(max_length=32, choices=Capability.choices), default=list
    )
    context_window = models.PositiveIntegerField(help_text="Input plus output tokens.")
    max_output_tokens = models.PositiveIntegerField()
    accepts_temperature = models.BooleanField(
        default=False,
        help_text="Send the caller's sampling temperature. Off for models that only run at "
        "their default.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Inactive models are never routed to (kill switch, < 1 minute)."
    )
    notes = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_models"
        ordering = ("provider", "provider_model")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_model"], name="ai_models_provider_model_uniq"
            ),
            models.CheckConstraint(
                condition=Q(max_output_tokens__lte=F("context_window")),
                name="ai_models_output_within_window_chk",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}/{self.provider_model}"

    def current_price(self, at: Any = None) -> "AIModelPrice | None":
        moment = at or timezone.now()
        return self.prices.filter(effective_from__lte=moment).order_by("-effective_from").first()


class AIModelPrice(models.Model):
    """A price version. The price in force at time T is the row with the latest
    `effective_from <= T`; a price change is a new row, and usage rows keep the price they
    were charged with. Prices are micro-dollars per million tokens ($3.00 = 3_000_000)."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    model = models.ForeignKey(AIModel, on_delete=models.PROTECT, related_name="prices")
    input_micros_per_million = models.BigIntegerField()
    output_micros_per_million = models.BigIntegerField()
    cached_input_micros_per_million = models.BigIntegerField(default=0)
    cache_write_micros_per_million = models.BigIntegerField(default=0)
    effective_from = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=240, blank=True, help_text="Where the price came from.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_model_prices"
        ordering = ("model", "-effective_from")
        constraints = [
            models.UniqueConstraint(
                fields=["model", "effective_from"], name="ai_model_prices_model_from_uniq"
            ),
            models.CheckConstraint(
                condition=Q(input_micros_per_million__gte=0)
                & Q(output_micros_per_million__gte=0)
                & Q(cached_input_micros_per_million__gte=0)
                & Q(cache_write_micros_per_million__gte=0),
                name="ai_model_prices_non_negative_chk",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.model} from {self.effective_from:%Y-%m-%d}"


class AIModelAlias(models.Model):
    """A logical model such as `nexa-balanced`. Application code only ever names these."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    name = models.CharField(max_length=48, unique=True, validators=[alias_name_validator])
    display_name = models.CharField(max_length=80)
    description = models.CharField(max_length=240, blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False, help_text="Used when a request names no model. At most one alias."
    )
    available_by_default = models.BooleanField(
        default=True,
        help_text="Every organization may use it. Otherwise only organizations with a grant.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_model_aliases"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"], condition=Q(is_default=True), name="ai_aliases_one_default"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class AIAliasTarget(models.Model):
    """One candidate model for an alias; priority 1 is preferred, the rest are fallbacks."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    alias = models.ForeignKey(AIModelAlias, on_delete=models.CASCADE, related_name="targets")
    model = models.ForeignKey(AIModel, on_delete=models.PROTECT, related_name="alias_targets")
    priority = models.PositiveSmallIntegerField()

    class Meta:
        db_table = "ai_alias_targets"
        ordering = ("alias", "priority")
        constraints = [
            models.UniqueConstraint(fields=["alias", "priority"], name="ai_targets_priority_uniq"),
            models.UniqueConstraint(fields=["alias", "model"], name="ai_targets_model_uniq"),
            models.CheckConstraint(condition=Q(priority__gte=1), name="ai_targets_priority_chk"),
        ]

    def __str__(self) -> str:
        return f"{self.alias} #{self.priority}: {self.model}"


class AIOrganizationPolicy(models.Model):
    """AI settings for one organization. Edited by platform staff only, never by the tenant,
    so an organization cannot raise its own limits. Without a row, the platform defaults
    from Runtime's settings apply. Empty limits mean "use the platform default"."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, primary_key=True, related_name="ai_policy"
    )
    ai_enabled = models.BooleanField(default=True)
    requests_per_minute = models.PositiveIntegerField(null=True, blank=True)
    requests_per_day = models.PositiveIntegerField(null=True, blank=True)
    tokens_per_day = models.PositiveBigIntegerField(null=True, blank=True)
    max_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    concurrent_requests = models.PositiveSmallIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=240, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_organization_policies"
        verbose_name = "AI organization policy"
        verbose_name_plural = "AI organization policies"

    def __str__(self) -> str:
        return f"AI policy for {self.organization_id}"


class AIOrganizationAliasGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="ai_alias_grants"
    )
    alias = models.ForeignKey(AIModelAlias, on_delete=models.CASCADE, related_name="grants")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_organization_alias_grants"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "alias"], name="ai_alias_grants_org_alias_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.alias} for {self.organization_id}"


class AIUsageEvent(models.Model):
    """One AI Gateway call: metadata only, never prompt or completion text.

    Written by Runtime; `id` is the gateway call ID. Identifiers are plain UUID columns, not
    foreign keys, so the ledger survives the deletion of an organization, user or model (as
    `audit_logs` does), and nothing points into it, which keeps monthly partitioning (Phase
    18) possible. A database trigger refuses UPDATE and DELETE for every role.
    """

    class Status(models.TextChoices):
        SUCCESS = "success"
        ERROR = "error"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    organization_id = models.UUIDField()
    user_id = models.UUIDField()
    actor_type = models.CharField(max_length=16, default="user")
    request_id = models.CharField(max_length=128, blank=True)
    source_type = models.CharField(max_length=32, blank=True, default="")
    source_id = models.UUIDField(null=True, blank=True)
    feature = models.CharField(max_length=32)
    alias = models.CharField(max_length=48)
    provider = models.CharField(max_length=32)
    provider_model = models.CharField(max_length=128)
    model_id = models.UUIDField(null=True, blank=True)
    price_id = models.UUIDField(null=True, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cached_input_tokens = models.PositiveIntegerField(default=0)
    cache_write_input_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(
        null=True, blank=True, help_text="Null when the model had no price."
    )
    latency_ms = models.PositiveIntegerField(default=0)
    ttft_ms = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_code = models.CharField(max_length=48, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=1)
    fallback_used = models.BooleanField(default=False)
    stream = models.BooleanField(default=False)
    structured = models.BooleanField(default=False)
    usage_source = models.CharField(max_length=16, default="provider")

    class Meta:
        db_table = "ai_usage_events"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["organization_id", "-created_at"], name="ai_usage_org_idx"),
            models.Index(fields=["user_id", "-created_at"], name="ai_usage_user_idx"),
            models.Index(fields=["request_id"], name="ai_usage_request_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_tokens=F("input_tokens") + F("output_tokens")),
                name="ai_usage_total_tokens_chk",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["success", "error", "cancelled"]),
                name="ai_usage_status_chk",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.alias} {self.status} at {self.created_at:%Y-%m-%d %H:%M:%S}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise RuntimeError("AI usage events are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise RuntimeError("AI usage events are append-only")
