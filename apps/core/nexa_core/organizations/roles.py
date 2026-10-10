"""Keep the database's permissions and system roles identical to `access.catalog`."""

from collections.abc import Iterable
from typing import Any

from django.db import transaction

from nexa_core.access.catalog import ALL_PERMISSIONS, PERMISSIONS, ROLE_TEMPLATES
from nexa_core.organizations.models import Organization, Permission, Role, RolePermission


def sync_permissions() -> dict[str, Permission]:
    """Upsert every catalog permission and delete permissions no longer in the catalog."""
    for spec in PERMISSIONS:
        Permission.objects.update_or_create(
            codename=spec.codename,
            defaults={"category": spec.category, "description": spec.description},
        )
    Permission.objects.exclude(codename__in=ALL_PERMISSIONS).delete()
    return {permission.codename: permission for permission in Permission.objects.all()}


def set_role_permissions(
    role: Role, codenames: Iterable[str], permissions: dict[str, Permission]
) -> tuple[set[str], set[str]]:
    """Make `role` hold exactly `codenames`. Returns (added, removed)."""
    desired = set(codenames)
    current = set(role.role_permissions.values_list("permission__codename", flat=True))
    added, removed = desired - current, current - desired
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, permission=permissions[code]) for code in sorted(added)]
    )
    if removed:
        role.role_permissions.filter(permission__codename__in=removed).delete()
    return added, removed


def create_system_roles(
    organization: Organization, permissions: dict[str, Permission] | None = None
) -> dict[str, Role]:
    permissions = permissions or {p.codename: p for p in Permission.objects.all()}
    roles: dict[str, Role] = {}
    for template in ROLE_TEMPLATES:
        role = Role.objects.create(
            organization=organization,
            key=template.key,
            name=template.name,
            description=template.description,
            is_system=True,
        )
        set_role_permissions(role, template.permissions, permissions)
        roles[template.key] = role
    return roles


@transaction.atomic
def sync_access_catalog() -> None:
    """Bring permissions and every organization's system roles in line with the catalog."""
    permissions = sync_permissions()
    templates = {template.key: template for template in ROLE_TEMPLATES}
    for role in Role.objects.filter(is_system=True):
        template = templates.get(role.key)
        if template is not None:
            set_role_permissions(role, template.permissions, permissions)


def sync_after_migrate(**_: Any) -> None:
    sync_access_catalog()
