"""`GET /api/v1/ai/usage/`: AI usage for the active organization.

Core serves this endpoint because Core owns the usage records and the analytics permissions;
Runtime only appends to the ledger. The edge routes `/api/v1/ai/usage` to Core and the rest of
`/api/v1/ai/` to Runtime (docs/adr/029-ai-requests-through-runtime.md).

    ?scope=me             your own usage in this organization (needs ai.chat)
    ?scope=organization   everyone's usage in this organization (needs analytics.view)
    ?days=30              1 to 90
"""

from rest_framework.request import Request
from rest_framework.response import Response

from nexa_common.access import require
from nexa_common.errors import FieldError, PermissionDenied, ValidationFailed
from nexa_core.access.drf import AuthenticatedView, current_auth, current_user
from nexa_core.access.principal import load_organization_context
from nexa_core.ai.usage import MAX_DAYS, summarize
from nexa_core.api import ok


class UsageView(AuthenticatedView):
    def get(self, request: Request) -> Response:
        auth = current_auth(request)
        organization_id = auth.claims.organization_id
        if organization_id is None:
            raise PermissionDenied("Select an organization first.")
        context = load_organization_context(current_user(request), organization_id, auth.session.id)

        scope = request.query_params.get("scope", "me")
        if scope not in ("me", "organization"):
            raise ValidationFailed(
                details=[
                    FieldError(field="scope", code="invalid", message="Use 'me' or 'organization'.")
                ]
            )
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            days = 0
        if not 1 <= days <= MAX_DAYS:
            raise ValidationFailed(
                details=[FieldError(field="days", code="invalid", message=f"Use 1 to {MAX_DAYS}.")]
            )

        if scope == "organization":
            require(context.principal, "analytics.view")
            user_id = None
        else:
            require(context.principal, "ai.chat")
            user_id = context.principal.user_id

        summary = summarize(organization_id=organization_id, user_id=user_id, days=days)
        return ok({"scope": scope, "organization_id": str(organization_id), **summary})
