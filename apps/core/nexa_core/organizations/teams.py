"""Teams: the first resource that uses all three authorization layers.

- Tenant: a team ID from another organization is not found.
- Role: `team.update`, `team.delete` and `team.manage_members` set the ceiling.
- Resource: managing a team's members needs MANAGE access to that team, which comes from
  leading it or from `team.manage_all` (Managers and above). So a Member who leads "Research"
  can add people to Research, but not to "Sales".
"""

from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from nexa_common.access import Access, ResourceRef, authorize
from nexa_common.errors import Conflict, NotFound
from nexa_core.access.principal import OrganizationContext
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.organizations.models import Team, TeamMembership
from nexa_core.organizations.services import get_membership


def team_resource(context: OrganizationContext, team: Team) -> ResourceRef:
    principal = context.principal
    leads = {principal.user_id: Access.MANAGE} if team.id in principal.led_team_ids else {}
    return ResourceRef(type="team", id=team.id, organization_id=team.organization_id, grants=leads)


def get_team(context: OrganizationContext, team_id: UUID) -> Team:
    team = Team.objects.filter(id=team_id, organization=context.organization).first()
    if team is None:
        raise NotFound
    return team


def _audit(context: OrganizationContext, action: AuditAction, team: Team, **metadata: str) -> None:
    record(
        action,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="team",
        resource_id=team.id,
        metadata=metadata,
    )


@transaction.atomic
def create_team(context: OrganizationContext, *, name: str, description: str) -> Team:
    try:
        with transaction.atomic():
            team = Team.objects.create(
                organization=context.organization,
                name=name.strip(),
                slug=slugify(name)[:64] or "team",
                description=description,
                created_by_id=context.principal.user_id,
            )
    except IntegrityError as exc:
        raise Conflict("A team with this name already exists.") from exc
    # The creator leads the new team.
    TeamMembership.objects.create(
        organization=context.organization,
        team=team,
        membership=context.membership,
        role=TeamMembership.TeamRole.LEAD,
    )
    _audit(context, AuditAction.TEAM_CREATED, team, name=team.name)
    return team


@transaction.atomic
def update_team(
    context: OrganizationContext, team_id: UUID, *, name: str | None, description: str | None
) -> Team:
    team = get_team(context, team_id)
    authorize(context.principal, "update", team_resource(context, team))
    if name is not None:
        team.name = name.strip()
        team.slug = slugify(name)[:64] or "team"
    if description is not None:
        team.description = description
    try:
        with transaction.atomic():
            team.save()
    except IntegrityError as exc:
        raise Conflict("A team with this name already exists.") from exc
    _audit(context, AuditAction.TEAM_UPDATED, team)
    return team


@transaction.atomic
def delete_team(context: OrganizationContext, team_id: UUID) -> None:
    team = get_team(context, team_id)
    authorize(context.principal, "delete", team_resource(context, team))
    _audit(context, AuditAction.TEAM_DELETED, team, name=team.name)
    team.delete()


@transaction.atomic
def add_team_member(
    context: OrganizationContext, team_id: UUID, membership_id: UUID, role: str
) -> TeamMembership:
    team = get_team(context, team_id)
    authorize(context.principal, "manage_members", team_resource(context, team))
    membership = get_membership(context, membership_id)  # same-tenant only
    team_membership, created = TeamMembership.objects.update_or_create(
        team=team,
        membership=membership,
        defaults={"organization": context.organization, "role": role},
    )
    _audit(
        context,
        AuditAction.TEAM_MEMBER_ADDED,
        team,
        user_id=str(membership.user_id),
        role=role,
        change="added" if created else "updated",
    )
    return team_membership


@transaction.atomic
def remove_team_member(context: OrganizationContext, team_id: UUID, membership_id: UUID) -> None:
    team = get_team(context, team_id)
    authorize(context.principal, "manage_members", team_resource(context, team))
    deleted, _ = TeamMembership.objects.filter(team=team, membership_id=membership_id).delete()
    if not deleted:
        raise NotFound
    _audit(context, AuditAction.TEAM_MEMBER_REMOVED, team, membership_id=str(membership_id))
