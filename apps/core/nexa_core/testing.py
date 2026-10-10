"""Helpers shared by Core's tests. Not imported by application code."""

from collections.abc import Callable

from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization, OrganizationMembership, Role

PASSWORD = "correct-horse-battery-staple"

UserFactory = Callable[..., User]
OrgFactory = Callable[..., Organization]


def add_member(
    organization: Organization,
    user: User,
    role_key: str,
    status: str = OrganizationMembership.Status.ACTIVE,
) -> OrganizationMembership:
    role = Role.objects.get(organization=organization, key=role_key)
    return OrganizationMembership.objects.create(
        organization=organization, user=user, role=role, status=status
    )
