"""Custom roles. System roles are fixed; custom roles are bundles of catalog permissions that
may never exceed the permissions of the person creating or editing them."""

from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from nexa_common.errors import Conflict, FieldError, NotFound, PermissionDenied, ValidationFailed
from nexa_core.access import rules
from nexa_core.access.catalog import ALL_PERMISSIONS
from nexa_core.access.principal import OrganizationContext
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.organizations.models import Permission, Role
from nexa_core.organizations.roles import set_role_permissions


def _validate_permissions(codenames: list[str]) -> frozenset[str]:
    unknown = sorted(set(codenames) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationFailed(
            details=[
                FieldError(
                    field="permissions", code="unknown", message=f"Unknown permission {code}."
                )
                for code in unknown
            ]
        )
    return frozenset(codenames)


def _catalog() -> dict[str, Permission]:
    return {permission.codename: permission for permission in Permission.objects.all()}


def _get_custom_role(context: OrganizationContext, role_id: UUID) -> Role:
    role = Role.objects.filter(id=role_id, organization=context.organization).first()
    if role is None:
        raise NotFound
    if role.is_system:
        raise PermissionDenied("System roles can't be changed. Create a custom role instead.")
    # Editing a role that is more powerful than you would be an escalation path.
    rules.ensure_can_grant_permissions(context.principal, rules.role_permissions(role))
    return role


@transaction.atomic
def create_role(
    context: OrganizationContext, *, name: str, description: str, permissions: list[str]
) -> Role:
    granted = _validate_permissions(permissions)
    rules.ensure_can_grant_permissions(context.principal, granted)
    try:
        with transaction.atomic():
            role = Role.objects.create(
                organization=context.organization,
                key=f"custom-{slugify(name)[:40] or 'role'}",
                name=name.strip(),
                description=description,
                is_system=False,
            )
    except IntegrityError as exc:
        raise Conflict("A role with this name already exists.") from exc
    set_role_permissions(role, granted, _catalog())
    record(
        AuditAction.ROLE_CREATED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="role",
        resource_id=role.id,
        metadata={"name": role.name, "permissions": sorted(granted)},
    )
    return role


@transaction.atomic
def update_role(
    context: OrganizationContext,
    role_id: UUID,
    *,
    name: str | None,
    description: str | None,
    permissions: list[str] | None,
) -> Role:
    role = _get_custom_role(context, role_id)
    if name is not None:
        role.name = name.strip()
    if description is not None:
        role.description = description
    try:
        with transaction.atomic():
            role.save()
    except IntegrityError as exc:
        raise Conflict("A role with this name already exists.") from exc

    record(
        AuditAction.ROLE_UPDATED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="role",
        resource_id=role.id,
    )
    if permissions is not None:
        granted = _validate_permissions(permissions)
        rules.ensure_can_grant_permissions(context.principal, granted)
        added, removed = set_role_permissions(role, granted, _catalog())
        if added or removed:
            record(
                AuditAction.PERMISSION_CHANGED,
                actor_id=context.principal.user_id,
                organization_id=context.organization.id,
                resource_type="role",
                resource_id=role.id,
                metadata={"added": sorted(added), "removed": sorted(removed)},
            )
    return role


@transaction.atomic
def delete_role(context: OrganizationContext, role_id: UUID) -> None:
    role = _get_custom_role(context, role_id)
    if role.memberships.exists():
        raise Conflict("Move the members with this role to another role first.")
    record(
        AuditAction.ROLE_DELETED,
        actor_id=context.principal.user_id,
        organization_id=context.organization.id,
        resource_type="role",
        resource_id=role.id,
        metadata={"name": role.name},
    )
    role.delete()
