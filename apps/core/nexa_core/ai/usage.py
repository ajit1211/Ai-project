"""Usage summaries read from the ledger. Always scoped to one organization, and to one user
unless the caller may see the whole organization's usage."""

from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from nexa_core.ai.models import AIUsageEvent

MAX_DAYS = 90
RECENT_LIMIT = 20


def usd(micros: int | None) -> str | None:
    if micros is None:
        return None
    return str((Decimal(micros) / 1_000_000).quantize(Decimal("0.000001")))


def summarize(*, organization_id: UUID, user_id: UUID | None, days: int) -> dict[str, Any]:
    since = timezone.now() - timedelta(days=days)
    events: QuerySet[AIUsageEvent] = AIUsageEvent.objects.filter(
        organization_id=organization_id, created_at__gte=since
    )
    if user_id is not None:
        events = events.filter(user_id=user_id)

    # Aliases must not shadow column names used inside the same aggregation.
    sums = {
        "n_requests": Count("id"),
        "n_errors": Count("id", filter=Q(status=AIUsageEvent.Status.ERROR)),
        "sum_input": Sum("input_tokens"),
        "sum_output": Sum("output_tokens"),
        "sum_total": Sum("total_tokens"),
        "sum_cost": Sum("cost_micros"),
        "n_unpriced": Count("id", filter=Q(cost_micros__isnull=True, total_tokens__gt=0)),
    }
    Rows = list[dict[str, Any]]
    totals = events.aggregate(**sums)
    by_model = cast(
        Rows, list(events.values("alias", "provider", "provider_model").annotate(**sums))
    )
    by_day = cast(
        Rows,
        list(
            events.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(**sums)
            .order_by("day")
        ),
    )
    recent = events.order_by("-created_at")[:RECENT_LIMIT]

    return {
        "period": {"days": days, "since": since.isoformat()},
        "totals": _row(totals),
        "by_model": [
            {
                "alias": row["alias"],
                "provider": row["provider"],
                "provider_model": row["provider_model"],
                **_row(row),
            }
            for row in sorted(by_model, key=lambda r: -(r["sum_total"] or 0))
        ],
        "by_day": [{"date": row["day"].isoformat(), **_row(row)} for row in by_day],
        "recent": [_event(event) for event in recent],
    }


def _row(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "requests": values["n_requests"] or 0,
        "errors": values["n_errors"] or 0,
        "input_tokens": values["sum_input"] or 0,
        "output_tokens": values["sum_output"] or 0,
        "total_tokens": values["sum_total"] or 0,
        "cost_usd": usd(values["sum_cost"] or 0),
        "unpriced_requests": values["n_unpriced"] or 0,
    }


def _event(event: AIUsageEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "created_at": event.created_at.isoformat(),
        "user_id": str(event.user_id),
        "feature": event.feature,
        "alias": event.alias,
        "provider": event.provider,
        "provider_model": event.provider_model,
        "status": event.status,
        "error_code": event.error_code or None,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "total_tokens": event.total_tokens,
        "cost_usd": usd(event.cost_micros),
        "latency_ms": event.latency_ms,
        "ttft_ms": event.ttft_ms,
        "stream": event.stream,
        "fallback_used": event.fallback_used,
    }
