"""DRF building blocks so views declare authorization instead of implementing it.

    class TeamListView(OrganizationScopedView):
        required_permissions = {"GET": "team.view", "POST": "team.create"}

The base view authenticates the caller, loads their membership in the organization named by
the URL (404 if none), and checks the permission for the HTTP method before the handler runs.
Handlers then use `self.principal` and `self.organization`; resource-level checks call
`nexa_common.access.authorize`.
"""

from typing import Any, ClassVar
from uuid import UUID

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from nexa_common.access import Principal, require
from nexa_core.access.authentication import AuthContext
from nexa_core.access.principal import OrganizationContext, load_organization_context
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization, OrganizationMembership


def current_user(request: Request) -> User:
    user = request.user
    assert isinstance(user, User), "only used behind IsAuthenticated"  # noqa: S101
    return user


def current_auth(request: Request) -> AuthContext:
    auth = request.auth
    assert isinstance(auth, AuthContext), "only used behind IsAuthenticated"  # noqa: S101
    return auth


class AuthenticatedView(APIView):
    permission_classes = (IsAuthenticated,)


class OrganizationScopedView(AuthenticatedView):
    """Base for every `/organizations/{organization_id}/...` endpoint."""

    required_permissions: ClassVar[dict[str, str]] = {}
    context: OrganizationContext

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        organization_id: UUID = kwargs["organization_id"]
        self.context = load_organization_context(
            current_user(request), organization_id, current_auth(request).session.id
        )
        permission = self.required_permissions.get(request.method or "")
        if permission:
            require(self.context.principal, permission)

    @property
    def principal(self) -> Principal:
        return self.context.principal

    @property
    def organization(self) -> Organization:
        return self.context.organization

    @property
    def membership(self) -> OrganizationMembership:
        return self.context.membership
