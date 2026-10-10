"""Rules that stop privilege escalation. Every member, role and invitation change calls these.

1. You can only hand out what you hold: a role (or invitation) may carry only permissions the
   actor already has. A Manager cannot invite an Admin; an Admin cannot create a role with
   `organization.delete`.
2. Owners are special: only an Owner can create, change or remove another Owner.
3. Nobody manages themselves through these endpoints: you cannot change your own role or
   status, or remove yourself (leaving has its own endpoint).
4. An organization never loses its last active Owner.
"""

from nexa_common.access import Principal
from nexa_common.errors import Conflict, PermissionDenied
from nexa_core.access.catalog import OWNER
from nexa_core.organizations.models import OrganizationMembership, Role, RolePermission


def role_permissions(role: Role) -> frozenset[str]:
    return frozenset(
        RolePermission.objects.filter(role=role).values_list("permission__codename", flat=True)
    )


def ensure_can_grant(actor: Principal, role: Role) -> None:
    if role.key == OWNER and actor.role_key != OWNER:
        raise PermissionDenied("Only owners can grant the Owner role.")
    if not role_permissions(role) <= actor.permissions:
        raise PermissionDenied("You can't grant a role with permissions you don't have.")


def ensure_can_grant_permissions(actor: Principal, permissions: frozenset[str]) -> None:
    if not permissions <= actor.permissions:
        raise PermissionDenied("You can't grant permissions you don't have.")


def ensure_can_manage_member(actor: Principal, target: OrganizationMembership) -> None:
    if target.user_id == actor.user_id:
        raise PermissionDenied("You can't change your own membership.")
    if target.role.key == OWNER and actor.role_key != OWNER:
        raise PermissionDenied("Only owners can change another owner.")
    if not role_permissions(target.role) <= actor.permissions:
        raise PermissionDenied("You can't manage a member who has permissions you don't have.")


def ensure_not_last_owner(membership: OrganizationMembership) -> None:
    if membership.role.key != OWNER or membership.status != OrganizationMembership.Status.ACTIVE:
        return
    owners = OrganizationMembership.objects.filter(
        organization_id=membership.organization_id,
        role__key=OWNER,
        status=OrganizationMembership.Status.ACTIVE,
    )
    if owners.count() <= 1:
        raise Conflict("An organization must keep at least one active owner.")
