"""Organizations and their members.

Callers pass an `OrganizationContext` (the actor's verified membership), never a bare ID, so
every function here already knows the tenant and the actor's permissions. Lookups of
memberships, roles and teams are always filtered by that tenant: an ID from another
organization is simply not found.
"""

import secrets
import string
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from nexa_common.errors import Conflict, FieldError, NotFound, ValidationFailed
from nexa_core.access import rules
from nexa_core.access.catalog import OWNER
from nexa_core.access.principal import OrganizationContext
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization, OrganizationMembership, Role
from nexa_core.organizations.roles import create_system_roles

_SLUG_ALPHABET = string.ascii_lowercase + string.digits


class OrganizationSettings(BaseModel):
    """The schema of `Organization.settings`. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    invitation_expiry_days: int = Field(default=7, ge=1, le=30)


def organization_settings(organization: Organization) -> OrganizationSettings:
    return OrganizationSettings.model_validate(organization.settings or {})


def _unique_slug(name: str) -> str:
    """A readable slug with a random suffix, so two organizations never race for one name."""
    suffix = "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(6))
    return f"{slugify(name)[:48] or 'org'}-{suffix}"


def _audit(context: OrganizationContext, action: AuditAction, **fields: Any) -> None:
    record(
        action,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        **fields,
    )


# --- organizations -----------------------------------------------------------------------


@transaction.atomic
def create_organization(
    *, name: str, owner: User, kind: str = Organization.Kind.TEAM
) -> Organization:
    organization = Organization.objects.create(
        name=name.strip(), slug=_unique_slug(name), kind=kind, created_by=owner
    )
    roles = create_system_roles(organization)
    OrganizationMembership.objects.create(organization=organization, user=owner, role=roles[OWNER])
    record(
        AuditAction.ORGANIZATION_CREATED,
        actor_id=owner.id,
        organization_id=organization.id,
        resource_type="organization",
        resource_id=organization.id,
        metadata={"kind": kind},
    )
    return organization


def create_personal_organization(user: User) -> Organization:
    """Every user gets a personal workspace at signup, so tenancy works the same way for
    individuals and teams (ADR-006)."""
    name = f"{user.full_name or user.email.split('@')[0]}'s workspace"
    return create_organization(name=name, owner=user, kind=Organization.Kind.PERSONAL)


@transaction.atomic
def update_organization(
    context: OrganizationContext, *, name: str | None, settings: dict[str, Any] | None
) -> Organization:
    organization = context.organization
    changed: list[str] = []
    if name is not None and name.strip() != organization.name:
        organization.name = name.strip()
        changed.append("name")
    if settings is not None:
        merged = {**organization_settings(organization).model_dump(), **settings}
        try:
            organization.settings = OrganizationSettings.model_validate(merged).model_dump()
        except PydanticValidationError as exc:
            raise ValidationFailed(
                details=[
                    FieldError(
                        field="settings." + ".".join(str(part) for part in error["loc"]),
                        code=error["type"],
                        message=error["msg"],
                    )
                    for error in exc.errors()
                ]
            ) from exc
        changed.append("settings")
    if changed:
        organization.save()
        _audit(
            context,
            AuditAction.ORGANIZATION_UPDATED,
            resource_type="organization",
            resource_id=organization.id,
            metadata={"fields": changed},
        )
    return organization


@transaction.atomic
def delete_organization(context: OrganizationContext) -> None:
    """Soft delete: the organization disappears for everyone at once and is purged later."""
    organization = context.organization
    if organization.is_personal:
        raise Conflict("Personal workspaces can't be deleted.")
    organization.deleted_at = timezone.now()
    organization.save(update_fields=["deleted_at", "updated_at"])
    _audit(
        context,
        AuditAction.ORGANIZATION_DELETED,
        resource_type="organization",
        resource_id=organization.id,
    )


@transaction.atomic
def leave_organization(context: OrganizationContext) -> None:
    if context.organization.is_personal:
        raise Conflict("You can't leave your personal workspace.")
    rules.ensure_not_last_owner(context.membership)
    context.membership.delete()
    _audit(
        context,
        AuditAction.MEMBER_LEFT,
        resource_type="membership",
        resource_id=context.membership.id,
    )


# --- members -----------------------------------------------------------------------------


def get_membership(context: OrganizationContext, membership_id: UUID) -> OrganizationMembership:
    membership = (
        OrganizationMembership.objects.select_related("user", "role")
        .filter(id=membership_id, organization=context.organization)
        .first()
    )
    if membership is None:
        raise NotFound
    return membership


def get_role(context: OrganizationContext, role_id: UUID) -> Role:
    role = Role.objects.filter(id=role_id, organization=context.organization).first()
    if role is None:
        raise ValidationFailed(
            details=[FieldError(field="role_id", code="not_found", message="Unknown role.")]
        )
    return role


@transaction.atomic
def change_member_role(
    context: OrganizationContext, membership_id: UUID, role_id: UUID
) -> OrganizationMembership:
    membership = get_membership(context, membership_id)
    role = get_role(context, role_id)
    rules.ensure_can_manage_member(context.principal, membership)
    rules.ensure_can_grant(context.principal, role)
    if role.id == membership.role_id:
        return membership
    if membership.role.key == OWNER:
        rules.ensure_not_last_owner(membership)

    previous = membership.role.key
    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    _audit(
        context,
        AuditAction.MEMBER_ROLE_CHANGED,
        resource_type="membership",
        resource_id=membership.id,
        metadata={"user_id": str(membership.user_id), "from": previous, "to": role.key},
    )
    return membership


@transaction.atomic
def set_member_status(
    context: OrganizationContext, membership_id: UUID, status: str
) -> OrganizationMembership:
    """Suspend or reactivate. A suspended member keeps their data but loses all access at once,
    because every request re-checks the membership."""
    membership = get_membership(context, membership_id)
    rules.ensure_can_manage_member(context.principal, membership)
    if status == membership.status:
        return membership
    if status == OrganizationMembership.Status.SUSPENDED:
        rules.ensure_not_last_owner(membership)
    membership.status = status
    membership.save(update_fields=["status", "updated_at"])
    _audit(
        context,
        AuditAction.MEMBER_STATUS_CHANGED,
        resource_type="membership",
        resource_id=membership.id,
        metadata={"user_id": str(membership.user_id), "status": status},
    )
    return membership


@transaction.atomic
def remove_member(context: OrganizationContext, membership_id: UUID) -> None:
    membership = get_membership(context, membership_id)
    rules.ensure_can_manage_member(context.principal, membership)
    rules.ensure_not_last_owner(membership)
    membership.delete()  # team memberships go with it
    _audit(
        context,
        AuditAction.MEMBER_REMOVED,
        resource_type="membership",
        resource_id=membership_id,
        metadata={"user_id": str(membership.user_id)},
    )
