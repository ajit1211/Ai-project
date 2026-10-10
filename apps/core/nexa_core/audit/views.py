"""Audit log API. Organization admins see their organization's log; nobody sees another's."""

from typing import Any
from uuid import UUID

from rest_framework.request import Request
from rest_framework.response import Response

from nexa_core.access.drf import OrganizationScopedView
from nexa_core.api import ok
from nexa_core.audit.models import AuditLog

PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def audit_payload(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "action": entry.action,
        "category": entry.category,
        "severity": entry.severity,
        "actor_type": entry.actor_type,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "ip_address": entry.ip_address,
        "metadata": entry.metadata,
        "created_at": entry.created_at.isoformat(),
    }


class AuditLogListView(OrganizationScopedView):
    required_permissions = {"GET": "audit.view"}

    def get(self, request: Request, organization_id: UUID) -> Response:
        entries = AuditLog.objects.filter(organization_id=self.organization.id)
        if category := request.query_params.get("category"):
            entries = entries.filter(category=category)
        if action := request.query_params.get("action"):
            entries = entries.filter(action=action)
        if actor := request.query_params.get("actor_id"):
            entries = entries.filter(actor_id=actor)

        # Keyset pagination on the time-ordered UUIDv7 primary key.
        if cursor := request.query_params.get("cursor"):
            entries = entries.filter(id__lt=cursor)
        try:
            limit = min(int(request.query_params.get("limit", PAGE_SIZE)), MAX_PAGE_SIZE)
        except ValueError:
            limit = PAGE_SIZE
        page = list(entries.order_by("-id")[: limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        return ok(
            [audit_payload(entry) for entry in page],
            meta={
                "next_cursor": str(page[-1].id) if has_more and page else None,
                "has_more": has_more,
            },
        )
