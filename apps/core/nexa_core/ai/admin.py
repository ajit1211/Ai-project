"""Platform-staff administration of the AI catalog and organization AI policies.

Kill switches: deactivating a model (or every model of a provider at once) stops new requests
from being routed to it within Runtime's registry refresh interval (30 seconds).
"""

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from nexa_core.ai.models import (
    AIAliasTarget,
    AIModel,
    AIModelAlias,
    AIModelPrice,
    AIOrganizationAliasGrant,
    AIOrganizationPolicy,
    AIUsageEvent,
)


class AIModelPriceInline(admin.TabularInline[AIModelPrice, AIModel]):
    model = AIModelPrice
    extra = 0
    fields = (
        "effective_from",
        "input_micros_per_million",
        "output_micros_per_million",
        "cached_input_micros_per_million",
        "cache_write_micros_per_million",
        "source",
    )

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False  # a price change is a new row; usage rows point at the old one


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin[AIModel]):
    list_display = (
        "provider_model",
        "provider",
        "display_name",
        "is_active",
        "context_window",
        "max_output_tokens",
        "accepts_temperature",
    )
    list_filter = ("provider", "is_active")
    search_fields = ("provider_model", "display_name")
    inlines = (AIModelPriceInline,)
    actions = ("deactivate", "activate", "deactivate_provider")

    @admin.action(description="Deactivate selected models (kill switch)")
    def deactivate(self, request: HttpRequest, queryset: QuerySet[AIModel]) -> None:
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} model(s) deactivated.", messages.WARNING)

    @admin.action(description="Activate selected models")
    def activate(self, request: HttpRequest, queryset: QuerySet[AIModel]) -> None:
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} model(s) activated.")

    @admin.action(description="Deactivate EVERY model of the selected models' providers")
    def deactivate_provider(self, request: HttpRequest, queryset: QuerySet[AIModel]) -> None:
        providers = set(queryset.values_list("provider", flat=True))
        count = AIModel.objects.filter(provider__in=providers).update(is_active=False)
        self.message_user(
            request,
            f"{count} model(s) from {', '.join(sorted(providers))} deactivated.",
            messages.WARNING,
        )


class AIAliasTargetInline(admin.TabularInline[AIAliasTarget, AIModelAlias]):
    model = AIAliasTarget
    extra = 0
    ordering = ("priority",)


@admin.register(AIModelAlias)
class AIModelAliasAdmin(admin.ModelAdmin[AIModelAlias]):
    list_display = ("name", "display_name", "is_active", "is_default", "available_by_default")
    list_filter = ("is_active", "available_by_default")
    inlines = (AIAliasTargetInline,)


@admin.register(AIOrganizationPolicy)
class AIOrganizationPolicyAdmin(admin.ModelAdmin[AIOrganizationPolicy]):
    list_display = (
        "organization",
        "ai_enabled",
        "requests_per_minute",
        "requests_per_day",
        "tokens_per_day",
        "concurrent_requests",
        "updated_at",
    )
    list_filter = ("ai_enabled",)
    search_fields = ("organization__name", "organization__slug")
    raw_id_fields = ("organization",)


@admin.register(AIOrganizationAliasGrant)
class AIOrganizationAliasGrantAdmin(admin.ModelAdmin[AIOrganizationAliasGrant]):
    list_display = ("organization", "alias", "created_at")
    list_filter = ("alias",)
    raw_id_fields = ("organization",)


@admin.register(AIUsageEvent)
class AIUsageEventAdmin(admin.ModelAdmin[AIUsageEvent]):
    """Read-only: the ledger is append-only."""

    list_display = (
        "created_at",
        "alias",
        "provider_model",
        "status",
        "total_tokens",
        "cost_micros",
        "latency_ms",
        "organization_id",
    )
    list_filter = ("status", "provider", "alias", "feature")
    search_fields = ("request_id", "organization_id", "user_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
