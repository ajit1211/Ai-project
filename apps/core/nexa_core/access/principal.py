"""Build a `Principal` from the database: never from anything the client sent.

The organization ID may come from the URL or from the access token's `org` claim; either
way it is only a claim until an active membership in a live organization is found here.
"""

from dataclasses import dataclass
from uuid import UUID

from nexa_common.access import Principal
from nexa_common.errors import NotFound
from nexa_core.identity.models import User
from nexa_core.organizations.models import (
    Organization,
    OrganizationMembership,
    RolePermission,
    TeamMembership,
)


@dataclass(frozen=True)
class OrganizationContext:
    principal: Principal
    organization: Organization
    membership: OrganizationMembership


def load_organization_context(
    user: User, organization_id: UUID, session_id: UUID | None
) -> OrganizationContext:
    """The caller's principal inside `organization_id`. Raises NotFound when the user is not an
    active member, so callers learn nothing about organizations they do not belong to."""
    membership = (
        OrganizationMembership.objects.select_related("organization", "role")
        .filter(
            user=user,
            organization_id=organization_id,
            status=OrganizationMembership.Status.ACTIVE,
            organization__deleted_at__isnull=True,
        )
        .first()
    )
    if membership is None:
        raise NotFound

    permissions = frozenset(
        RolePermission.objects.filter(role_id=membership.role_id).values_list(
            "permission__codename", flat=True
        )
    )
    teams = list(
        TeamMembership.objects.filter(membership=membership).values_list("team_id", "role")
    )
    principal = Principal(
        user_id=user.id,
        session_id=session_id,
        organization_id=membership.organization_id,
        membership_id=membership.id,
        role_key=membership.role.key,
        permissions=permissions,
        team_ids=frozenset(team_id for team_id, _ in teams),
        led_team_ids=frozenset(
            team_id for team_id, role in teams if role == TeamMembership.TeamRole.LEAD
        ),
    )
    return OrganizationContext(principal, membership.organization, membership)
