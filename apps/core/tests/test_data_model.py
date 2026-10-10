"""The database enforces tenancy and uniqueness rules on its own, whatever the code does."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from nexa_core.access.catalog import ALL_PERMISSIONS, ROLE_TEMPLATES_BY_KEY
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.models import AuditLog
from nexa_core.audit.service import record
from nexa_core.organizations.models import (
    Invitation,
    OrganizationMembership,
    Permission,
    Role,
    Team,
    TeamMembership,
)
from nexa_core.testing import OrgFactory, UserFactory, add_member

pytestmark = pytest.mark.django_db


def test_catalog_is_synced_to_the_database() -> None:
    assert set(Permission.objects.values_list("codename", flat=True)) == ALL_PERMISSIONS


def test_new_organization_gets_system_roles_and_an_owner(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    owner = make_user()
    org = make_org(owner)

    roles = {role.key: role for role in org.roles.all()}
    assert set(roles) == {"owner", "admin", "manager", "member", "viewer"}
    for key, role in roles.items():
        granted = set(role.role_permissions.values_list("permission__codename", flat=True))
        assert granted == ROLE_TEMPLATES_BY_KEY[key].permissions
    membership = OrganizationMembership.objects.get(organization=org, user=owner)
    assert membership.role.key == "owner"


def test_every_user_gets_a_profile(make_user: UserFactory) -> None:
    assert make_user().profile.timezone == "UTC"


def test_a_user_joins_an_organization_only_once(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    owner = make_user()
    org = make_org(owner)
    with pytest.raises(IntegrityError), transaction.atomic():
        add_member(org, owner, "member")


def test_membership_cannot_use_another_organizations_role(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    org_a, org_b = make_org(make_user()), make_org(make_user())
    foreign_role = Role.objects.get(organization=org_b, key="admin")
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizationMembership.objects.create(
            organization=org_a, user=make_user(), role=foreign_role
        )


def test_team_membership_cannot_mix_organizations(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    org_a, org_b = make_org(make_user()), make_org(make_user())
    team_in_a = Team.objects.create(organization=org_a, name="Research", slug="research")
    member_of_b = add_member(org_b, make_user(), "member")
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamMembership.objects.create(organization=org_a, team=team_in_a, membership=member_of_b)


def test_role_names_are_unique_per_organization_ignoring_case(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    org = make_org(make_user())
    with pytest.raises(IntegrityError), transaction.atomic():
        Role.objects.create(organization=org, key="owner-2", name="OWNER")
    # The same name is fine in another organization: each org has its own roles.
    assert make_org(make_user()).roles.filter(name="Owner").exists()


def test_only_one_pending_invitation_per_email(
    make_user: UserFactory, make_org: OrgFactory
) -> None:
    owner = make_user()
    org = make_org(owner)
    role = org.roles.get(key="member")
    fields = {"organization": org, "email": "new@example.com", "role": role, "invited_by": owner}
    expires = timezone.now() + timedelta(days=7)
    Invitation.objects.create(token_hash="a" * 64, expires_at=expires, **fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        Invitation.objects.create(token_hash="b" * 64, expires_at=expires, **fields)
    # Once the first is no longer pending, a new invitation is allowed.
    Invitation.objects.filter(token_hash="a" * 64).update(status=Invitation.Status.REVOKED)
    Invitation.objects.create(token_hash="c" * 64, expires_at=expires, **fields)


def test_audit_log_is_append_only(make_user: UserFactory) -> None:
    user = make_user()
    entry = record(
        AuditAction.USER_LOGIN, actor_id=user.id, metadata={"password": "x", "client": "web"}
    )
    assert entry.metadata == {"client": "web"}
    with pytest.raises(RuntimeError):
        entry.save()
    with pytest.raises(RuntimeError):
        entry.delete()
    assert AuditLog.objects.filter(action="USER_LOGIN").count() == 1
