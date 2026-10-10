"""The authorization policy shared by Core and Runtime (docs/architecture/authorization.md).

A request is allowed only if every layer agrees:

1. Tenant:   the resource belongs to the caller's organization. Otherwise it does not exist,
             as far as the caller can tell (404, never 403).
2. Role:     the caller's role grants the permission `<resource>.<action>`. This is a ceiling:
             nothing below can raise it.
3. Resource: the caller's access to this particular resource is high enough, through
             ownership, team membership, an explicit grant, organization-wide visibility, or
             the `<resource>.manage_all` permission held by administrators.

AI agents use the same policy. `Principal.for_agent` intersects the user's permissions with
the agent's, so an agent can never do something its user could not (doc 14.5).

This module is pure: services load the data, this decides.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Literal
from uuid import UUID

from nexa_common.errors import NotFound, PermissionDenied

ActorType = Literal["user", "agent"]
Visibility = Literal["private", "team", "organization"]


class Access(IntEnum):
    NONE = 0
    VIEW = 1
    EDIT = 2
    MANAGE = 3


# The resource-level access each kind of action needs.
ACTION_ACCESS: Mapping[str, Access] = {
    "view": Access.VIEW,
    "execute": Access.VIEW,
    "update": Access.EDIT,
    "upload": Access.EDIT,
    "delete": Access.MANAGE,
    "share": Access.MANAGE,
    "manage_members": Access.MANAGE,
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting, in which organization, with which permissions. Built server-side only."""

    user_id: UUID
    session_id: UUID | None
    organization_id: UUID | None
    membership_id: UUID | None = None
    role_key: str | None = None
    permissions: frozenset[str] = frozenset()
    team_ids: frozenset[UUID] = frozenset()
    led_team_ids: frozenset[UUID] = frozenset()
    actor_type: ActorType = "user"
    agent_id: UUID | None = None

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def for_agent(self, agent_id: UUID, agent_permissions: Iterable[str]) -> "Principal":
        """The principal an agent acts as: never more than its user, often less."""
        return replace(
            self,
            actor_type="agent",
            agent_id=agent_id,
            permissions=self.permissions & frozenset(agent_permissions),
        )


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """What the policy needs to know about one resource (a knowledge base, an agent, a team)."""

    type: str
    id: UUID
    organization_id: UUID
    owner_id: UUID | None = None
    visibility: Visibility = "organization"
    team_id: UUID | None = None
    # Explicit grants to users or teams, keyed by their ID.
    grants: Mapping[UUID, Access] = field(default_factory=dict)


def require(principal: Principal, permission: str) -> None:
    if not principal.has(permission):
        raise PermissionDenied


def resource_access(principal: Principal, resource: ResourceRef) -> Access:
    """The highest access the principal has to this one resource, ignoring role ceilings."""
    if principal.organization_id != resource.organization_id:
        return Access.NONE
    if principal.has(f"{resource.type}.manage_all"):
        return Access.MANAGE
    if resource.owner_id is not None and resource.owner_id == principal.user_id:
        return Access.MANAGE

    levels = [Access.NONE, resource.grants.get(principal.user_id, Access.NONE)]
    levels += [resource.grants.get(team_id, Access.NONE) for team_id in principal.team_ids]
    if resource.visibility == "organization" or (
        resource.visibility == "team" and resource.team_id in principal.team_ids
    ):
        levels.append(Access.VIEW)
    return max(levels)


def can(principal: Principal, action: str, resource: ResourceRef) -> bool:
    if principal.organization_id != resource.organization_id:
        return False
    if not principal.has(f"{resource.type}.{action}"):
        return False
    return resource_access(principal, resource) >= ACTION_ACCESS.get(action, Access.MANAGE)


def authorize(principal: Principal, action: str, resource: ResourceRef) -> None:
    """Raise NotFound for another tenant's resource (no existence leak), PermissionDenied for
    a resource the caller can see but not act on, and return normally otherwise."""
    if principal.organization_id != resource.organization_id:
        raise NotFound
    if not can(principal, action, resource):
        if resource_access(principal, resource) == Access.NONE:
            raise NotFound
        raise PermissionDenied
