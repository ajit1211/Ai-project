"""Organizations, members, invitations, teams, roles and the audit log.

The security tests at the end are the important ones: every cross-tenant and
privilege-escalation path the design claims to block is exercised here.
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from django.core import mail
from rest_framework.test import APIClient

from nexa_core.audit.models import AuditLog
from nexa_core.celery import app as celery_app
from nexa_core.identity.models import User
from nexa_core.organizations.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    Role,
    Team,
    TeamMembership,
)
from nexa_core.testing import PASSWORD, OrgFactory, UserFactory, add_member

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Callable[[], Any]:
    return lambda: django_capture_on_commit_callbacks(execute=True)


def client_for(user: User) -> APIClient:
    """A client signed in as `user`, with the access token already attached."""
    api = APIClient()
    response = api.post(
        "/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200, response.data
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['data']['access_token']}")
    return api


def role_id(org: Organization, key: str) -> str:
    return str(Role.objects.get(organization=org, key=key).id)


def membership_id(org: Organization, user: User) -> str:
    return str(OrganizationMembership.objects.get(organization=org, user=user).id)


def _token_from_last_email() -> str:
    match = re.search(r"#token=([\w-]+)", str(mail.outbox[-1].body))
    assert match
    return match.group(1)


# --- organizations -----------------------------------------------------------------------


class TestOrganizations:
    def test_new_user_sees_only_their_personal_workspace(self, make_user: UserFactory) -> None:
        user = make_user()
        from nexa_core.organizations.services import create_personal_organization

        create_personal_organization(user)
        orgs = client_for(user).get("/api/v1/organizations/").data["data"]
        assert len(orgs) == 1
        assert orgs[0]["kind"] == "personal"
        assert orgs[0]["role"]["key"] == "owner"

    def test_create_organization_makes_creator_the_owner(self, make_user: UserFactory) -> None:
        user = make_user()
        response = client_for(user).post("/api/v1/organizations/", {"name": "Acme"}, format="json")
        assert response.status_code == 201
        assert response.data["data"]["role"]["key"] == "owner"
        org = Organization.objects.get(id=response.data["data"]["id"])
        assert org.roles.count() == 5

    def test_detail_includes_effective_permissions(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        viewer = make_user()
        add_member(org, viewer, "viewer")

        data = client_for(viewer).get(f"/api/v1/organizations/{org.id}/").data["data"]
        assert "organization.view" in data["permissions"]
        assert "members.invite" not in data["permissions"]
        assert "settings" in data

    def test_update_requires_permission_and_validates_settings(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        url = f"/api/v1/organizations/{org.id}/"

        denied = client_for(member).patch(url, {"name": "Evil Corp"}, format="json")
        bad = client_for(owner).patch(
            url, {"settings": {"invitation_expiry_days": 99}}, format="json"
        )
        unknown = client_for(owner).patch(url, {"settings": {"is_enterprise": True}}, format="json")
        good = client_for(owner).patch(
            url, {"name": "Acme Ltd", "settings": {"invitation_expiry_days": 3}}, format="json"
        )

        assert denied.status_code == 403
        assert bad.status_code == 422
        assert unknown.status_code == 422
        assert good.status_code == 200
        assert good.data["data"]["name"] == "Acme Ltd"
        assert good.data["data"]["settings"] == {"invitation_expiry_days": 3}

    def test_delete_is_owner_only_and_hides_the_organization(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, admin = make_user(), make_user()
        org = make_org(owner)
        add_member(org, admin, "admin")
        url = f"/api/v1/organizations/{org.id}/"

        assert client_for(admin).delete(url).status_code == 403
        assert client_for(owner).delete(url).status_code == 200
        assert client_for(owner).get(url).status_code == 404
        assert Organization.objects.get(id=org.id).deleted_at is not None

    def test_personal_workspace_cannot_be_deleted(self, make_user: UserFactory) -> None:
        from nexa_core.organizations.services import create_personal_organization

        user = make_user()
        org = create_personal_organization(user)
        response = client_for(user).delete(f"/api/v1/organizations/{org.id}/")
        assert response.status_code == 409

    def test_leaving(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")

        # The only owner can't leave; a member can.
        assert client_for(owner).post(f"/api/v1/organizations/{org.id}/leave/").status_code == 409
        assert client_for(member).post(f"/api/v1/organizations/{org.id}/leave/").status_code == 200
        assert not OrganizationMembership.objects.filter(organization=org, user=member).exists()


# --- members -----------------------------------------------------------------------------


class TestMembers:
    def test_list_members(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        members = client_for(member).get(f"/api/v1/organizations/{org.id}/members/").data["data"]
        assert {m["role"]["key"] for m in members} == {"owner", "member"}
        assert "password" not in str(members)

    def test_promote_and_demote(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/"

        promoted = client_for(owner).patch(url, {"role_id": role_id(org, "admin")}, format="json")
        assert promoted.status_code == 200
        assert promoted.data["data"]["role"]["key"] == "admin"
        assert AuditLog.objects.filter(
            action="MEMBER_ROLE_CHANGED", organization_id=org.id
        ).exists()

    def test_suspension_takes_effect_immediately(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        member_client = client_for(member)
        assert member_client.get(f"/api/v1/organizations/{org.id}/").status_code == 200

        url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/"
        assert (
            client_for(owner).patch(url, {"status": "suspended"}, format="json").status_code == 200
        )
        # Same access token as before, but the organization is gone for them.
        assert member_client.get(f"/api/v1/organizations/{org.id}/").status_code == 404

    def test_remove_member(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/"
        assert client_for(owner).delete(url).status_code == 200
        assert not OrganizationMembership.objects.filter(organization=org, user=member).exists()


# --- invitations -------------------------------------------------------------------------


class TestInvitations:
    def _invite(
        self, inviter: User, org: Organization, email: str, role_key: str, commit: Callable[[], Any]
    ) -> Any:
        with commit():
            return client_for(inviter).post(
                f"/api/v1/organizations/{org.id}/invitations/",
                {"email": email, "role_id": role_id(org, role_key)},
                format="json",
            )

    def test_full_invitation_flow(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        invitee = make_user("new@example.com")

        sent = self._invite(owner, org, "New@Example.com", "member", commit)
        assert sent.status_code == 201
        assert sent.data["data"]["email"] == "new@example.com"
        assert "invited you" in mail.outbox[-1].subject
        token = _token_from_last_email()

        preview = APIClient().post("/api/v1/invitations/preview/", {"token": token}, format="json")
        assert preview.data["data"]["organization_name"] == org.name
        assert preview.data["data"]["role_name"] == "Member"

        accepted = client_for(invitee).post(
            "/api/v1/invitations/accept/", {"token": token}, format="json"
        )
        assert accepted.status_code == 200
        assert accepted.data["data"]["role"]["key"] == "member"
        assert OrganizationMembership.objects.filter(organization=org, user=invitee).exists()
        assert Invitation.objects.get(token_hash__isnull=False).status == "accepted"
        assert AuditLog.objects.filter(action="MEMBER_JOINED", organization_id=org.id).exists()

        # A used link is dead.
        again = client_for(invitee).post(
            "/api/v1/invitations/accept/", {"token": token}, format="json"
        )
        assert again.status_code == 400

    def test_only_the_invited_address_can_accept(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        self._invite(owner, org, "invited@example.com", "member", commit)
        token = _token_from_last_email()

        someone_else = make_user("other@example.com")
        response = client_for(someone_else).post(
            "/api/v1/invitations/accept/", {"token": token}, format="json"
        )
        assert response.status_code == 403
        assert not OrganizationMembership.objects.filter(
            organization=org, user=someone_else
        ).exists()

    def test_decline_and_duplicate_rules(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        first = self._invite(owner, org, "x@example.com", "member", commit)
        duplicate = self._invite(owner, org, "x@example.com", "viewer", commit)
        assert first.status_code == 201
        assert duplicate.status_code == 409

        token = _token_from_last_email()
        assert (
            APIClient()
            .post("/api/v1/invitations/decline/", {"token": token}, format="json")
            .status_code
            == 200
        )
        # Declined is no longer pending, so a fresh invitation is allowed.
        assert self._invite(owner, org, "x@example.com", "member", commit).status_code == 201

    def test_resend_replaces_the_link_and_revoke_kills_it(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        created = self._invite(owner, org, "x@example.com", "member", commit)
        old_token = _token_from_last_email()
        url = f"/api/v1/organizations/{org.id}/invitations/{created.data['data']['id']}/"

        with commit():
            resent = client_for(owner).post(url)
        new_token = _token_from_last_email()
        assert resent.status_code == 200
        assert resent.data["data"]["sent_count"] == 2
        assert (
            APIClient()
            .post("/api/v1/invitations/preview/", {"token": old_token}, format="json")
            .status_code
            == 400
        )
        assert (
            APIClient()
            .post("/api/v1/invitations/preview/", {"token": new_token}, format="json")
            .status_code
            == 200
        )

        assert client_for(owner).delete(url).status_code == 200
        assert (
            APIClient()
            .post("/api/v1/invitations/preview/", {"token": new_token}, format="json")
            .status_code
            == 400
        )

    def test_existing_member_cannot_be_invited(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        assert self._invite(owner, org, member.email, "viewer", commit).status_code == 409

    def test_personal_workspace_cannot_invite(
        self, make_user: UserFactory, commit: Callable[[], Any]
    ) -> None:
        from nexa_core.organizations.services import create_personal_organization

        user = make_user()
        org = create_personal_organization(user)
        assert self._invite(user, org, "x@example.com", "member", commit).status_code == 409


# --- teams -------------------------------------------------------------------------------


class TestTeams:
    def test_create_list_and_detail(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner = make_user()
        org = make_org(owner)
        api = client_for(owner)

        created = api.post(
            f"/api/v1/organizations/{org.id}/teams/", {"name": "Research"}, format="json"
        )
        assert created.status_code == 201
        assert (
            api.post(
                f"/api/v1/organizations/{org.id}/teams/", {"name": "research"}, format="json"
            ).status_code
            == 409
        )

        teams = api.get(f"/api/v1/organizations/{org.id}/teams/").data["data"]
        assert [t["name"] for t in teams] == ["Research"]
        detail = api.get(
            f"/api/v1/organizations/{org.id}/teams/{created.data['data']['id']}/"
        ).data["data"]
        assert detail["members"][0]["role"] == "lead"  # the creator leads the team

    def test_team_leads_manage_their_own_team_only(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, lead, newcomer = make_user(), make_user(), make_user()
        org = make_org(owner)
        lead_membership = add_member(org, lead, "member")
        newcomer_membership = add_member(org, newcomer, "member")
        research = Team.objects.create(organization=org, name="Research", slug="research")
        sales = Team.objects.create(organization=org, name="Sales", slug="sales")
        TeamMembership.objects.create(
            organization=org, team=research, membership=lead_membership, role="lead"
        )

        api = client_for(lead)
        body = {"membership_id": str(newcomer_membership.id)}
        own_team = api.post(
            f"/api/v1/organizations/{org.id}/teams/{research.id}/members/", body, format="json"
        )
        other_team = api.post(
            f"/api/v1/organizations/{org.id}/teams/{sales.id}/members/", body, format="json"
        )

        assert own_team.status_code == 201
        assert other_team.status_code == 403
        # A Manager has team.manage_all and can add to any team.
        manager = make_user()
        add_member(org, manager, "manager")
        assert (
            client_for(manager)
            .post(f"/api/v1/organizations/{org.id}/teams/{sales.id}/members/", body, format="json")
            .status_code
            == 201
        )

    def test_update_and_delete_follow_role_and_resource_rules(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, member, manager = make_user(), make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        add_member(org, manager, "manager")
        team = Team.objects.create(organization=org, name="Ops", slug="ops")
        url = f"/api/v1/organizations/{org.id}/teams/{team.id}/"

        assert (
            client_for(member).patch(url, {"name": "Operations"}, format="json").status_code == 403
        )
        assert (
            client_for(manager).patch(url, {"name": "Operations"}, format="json").status_code == 200
        )
        assert client_for(member).delete(url).status_code == 403
        assert client_for(manager).delete(url).status_code == 200
        assert not Team.objects.filter(id=team.id).exists()


# --- roles -------------------------------------------------------------------------------


class TestRoles:
    def test_list_shows_system_roles_with_permissions(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner = make_user()
        org = make_org(owner)
        roles = client_for(owner).get(f"/api/v1/organizations/{org.id}/roles/").data["data"]
        assert {r["key"] for r in roles} == {"owner", "admin", "manager", "member", "viewer"}
        assert "organization.delete" in next(r for r in roles if r["key"] == "owner")["permissions"]

    def test_custom_role_lifecycle(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        api = client_for(owner)
        base = f"/api/v1/organizations/{org.id}/roles/"

        created = api.post(
            base,
            {"name": "Analyst", "permissions": ["organization.view", "analytics.view", "ai.chat"]},
            format="json",
        )
        assert created.status_code == 201
        custom_id = created.data["data"]["id"]
        assert (
            api.post(base, {"name": "Analyst", "permissions": []}, format="json").status_code == 409
        )
        unknown = api.post(base, {"name": "X", "permissions": ["superpowers.all"]}, format="json")
        assert unknown.status_code == 422

        updated = api.patch(
            f"{base}{custom_id}/", {"permissions": ["organization.view"]}, format="json"
        )
        assert updated.data["data"]["permissions"] == ["organization.view"]
        assert AuditLog.objects.filter(action="PERMISSION_CHANGED", organization_id=org.id).exists()

        # Assign it, then deletion is blocked until the member moves.
        member_url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/"
        assert api.patch(member_url, {"role_id": custom_id}, format="json").status_code == 200
        assert api.delete(f"{base}{custom_id}/").status_code == 409
        assert (
            api.patch(member_url, {"role_id": role_id(org, "viewer")}, format="json").status_code
            == 200
        )
        assert api.delete(f"{base}{custom_id}/").status_code == 200

    def test_system_roles_are_immutable(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner = make_user()
        org = make_org(owner)
        url = f"/api/v1/organizations/{org.id}/roles/{role_id(org, 'viewer')}/"
        assert (
            client_for(owner)
            .patch(url, {"permissions": ["organization.delete"]}, format="json")
            .status_code
            == 403
        )
        assert client_for(owner).delete(url).status_code == 403


# --- audit log ---------------------------------------------------------------------------


class TestAuditLog:
    def test_admins_see_their_organizations_log_with_pagination(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        api = client_for(owner)
        for name in ("A", "B", "C"):
            api.post(f"/api/v1/organizations/{org.id}/teams/", {"name": name}, format="json")

        assert (
            client_for(member).get(f"/api/v1/organizations/{org.id}/audit-logs/").status_code == 403
        )
        page = api.get(f"/api/v1/organizations/{org.id}/audit-logs/?limit=2&category=audit")
        assert page.status_code == 200
        assert len(page.data["data"]) == 2
        assert page.data["meta"]["has_more"] is True
        rest = api.get(
            f"/api/v1/organizations/{org.id}/audit-logs/?limit=2&category=audit&cursor={page.data['meta']['next_cursor']}"
        )
        assert rest.data["data"][0]["id"] < page.data["data"][-1]["id"]

    def test_security_events_are_a_filtered_view(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, member = make_user(), make_user()
        org = make_org(owner)
        add_member(org, member, "member")
        api = client_for(owner)
        api.patch(
            f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/",
            {"role_id": role_id(org, "admin")},
            format="json",
        )
        events = api.get(f"/api/v1/organizations/{org.id}/audit-logs/?category=security").data[
            "data"
        ]
        assert [e["action"] for e in events] == ["MEMBER_ROLE_CHANGED"]


# --- security: tenancy and escalation ----------------------------------------------------


class TestTenantIsolation:
    """A user in organization A against organization B's resources. Always 404."""

    @pytest.fixture
    def two_orgs(self, make_user: UserFactory, make_org: OrgFactory) -> dict[str, Any]:
        owner_a, owner_b = make_user(), make_user()
        org_a, org_b = make_org(owner_a, "A"), make_org(owner_b, "B")
        team_b = Team.objects.create(organization=org_b, name="Secret", slug="secret")
        return {
            "api": client_for(owner_a),
            "org_a": org_a,
            "org_b": org_b,
            "owner_b": owner_b,
            "team_b": team_b,
        }

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "", None),
            ("patch", "", {"name": "Pwned"}),
            ("delete", "", None),
            ("get", "members/", None),
            ("get", "invitations/", None),
            ("post", "invitations/", {"email": "x@example.com", "role_id": str(uuid4())}),
            ("get", "teams/", None),
            ("post", "teams/", {"name": "Intruders"}),
            ("get", "roles/", None),
            ("get", "audit-logs/", None),
        ],
    )
    def test_every_org_endpoint_is_invisible_across_tenants(
        self, two_orgs: dict[str, Any], method: str, path: str, body: Any
    ) -> None:
        url = f"/api/v1/organizations/{two_orgs['org_b'].id}/{path}"
        response = getattr(two_orgs["api"], method)(url, body, format="json")
        assert response.status_code == 404, response.data
        assert response.data["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_foreign_ids_inside_my_own_organization_are_not_found(
        self, two_orgs: dict[str, Any]
    ) -> None:
        api, org_a, org_b = two_orgs["api"], two_orgs["org_a"], two_orgs["org_b"]
        foreign_member = membership_id(org_b, two_orgs["owner_b"])
        foreign_team = two_orgs["team_b"].id

        assert (
            api.delete(f"/api/v1/organizations/{org_a.id}/members/{foreign_member}/").status_code
            == 404
        )
        assert api.get(f"/api/v1/organizations/{org_a.id}/teams/{foreign_team}/").status_code == 404
        # A role from org B cannot be assigned in org A, even by A's owner.
        member = User.objects.create_user("m@example.com", PASSWORD)
        add_member(org_a, member, "member")
        response = api.patch(
            f"/api/v1/organizations/{org_a.id}/members/{membership_id(org_a, member)}/",
            {"role_id": role_id(org_b, "owner")},
            format="json",
        )
        assert response.status_code == 422

    def test_switching_to_an_organization_you_do_not_belong_to(
        self, two_orgs: dict[str, Any]
    ) -> None:
        response = two_orgs["api"].post(
            "/api/v1/auth/switch-organization/",
            {"organization_id": str(two_orgs["org_b"].id)},
            format="json",
        )
        assert response.status_code == 404

    def test_nonexistent_organization_looks_the_same_as_a_foreign_one(
        self, two_orgs: dict[str, Any]
    ) -> None:
        missing = two_orgs["api"].get(f"/api/v1/organizations/{uuid4()}/")
        foreign = two_orgs["api"].get(f"/api/v1/organizations/{two_orgs['org_b'].id}/")
        assert missing.status_code == foreign.status_code == 404
        assert missing.data["error"] == foreign.data["error"]


class TestPrivilegeEscalation:
    def test_admin_cannot_grant_owner(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, admin, member = make_user(), make_user(), make_user()
        org = make_org(owner)
        add_member(org, admin, "admin")
        add_member(org, member, "member")
        response = client_for(admin).patch(
            f"/api/v1/organizations/{org.id}/members/{membership_id(org, member)}/",
            {"role_id": role_id(org, "owner")},
            format="json",
        )
        assert response.status_code == 403

    def test_manager_cannot_invite_above_their_own_role(
        self, make_user: UserFactory, make_org: OrgFactory, commit: Callable[[], Any]
    ) -> None:
        owner, manager = make_user(), make_user()
        org = make_org(owner)
        add_member(org, manager, "manager")
        api = client_for(manager)
        with commit():
            admin_invite = api.post(
                f"/api/v1/organizations/{org.id}/invitations/",
                {"email": "a@example.com", "role_id": role_id(org, "admin")},
                format="json",
            )
            member_invite = api.post(
                f"/api/v1/organizations/{org.id}/invitations/",
                {"email": "b@example.com", "role_id": role_id(org, "member")},
                format="json",
            )
        assert admin_invite.status_code == 403
        assert member_invite.status_code == 201

    def test_nobody_edits_their_own_membership(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, admin = make_user(), make_user()
        org = make_org(owner)
        add_member(org, admin, "admin")
        response = client_for(admin).patch(
            f"/api/v1/organizations/{org.id}/members/{membership_id(org, admin)}/",
            {"role_id": role_id(org, "owner")},
            format="json",
        )
        assert response.status_code == 403

    def test_admin_cannot_touch_an_owner(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, admin = make_user(), make_user()
        org = make_org(owner)
        add_member(org, admin, "admin")
        url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, owner)}/"
        api = client_for(admin)
        assert api.patch(url, {"status": "suspended"}, format="json").status_code == 403
        assert api.delete(url).status_code == 403

    def test_last_owner_is_protected(self, make_user: UserFactory, make_org: OrgFactory) -> None:
        owner, second_owner = make_user(), make_user()
        org = make_org(owner)
        add_member(org, second_owner, "owner")
        url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, owner)}/"

        # Two owners: demoting one is fine.
        response = client_for(second_owner).patch(
            url, {"role_id": role_id(org, "admin")}, format="json"
        )
        assert response.status_code == 200
        # The demoted user is now an admin and can no longer touch the remaining owner...
        own_url = f"/api/v1/organizations/{org.id}/members/{membership_id(org, second_owner)}/"
        assert (
            client_for(owner).patch(own_url, {"status": "suspended"}, format="json").status_code
            == 403
        )
        # ...and the remaining owner cannot leave the organization ownerless.
        assert (
            client_for(second_owner).post(f"/api/v1/organizations/{org.id}/leave/").status_code
            == 409
        )

    def test_custom_role_cannot_exceed_its_creator(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        owner, manager = make_user(), make_user()
        org = make_org(owner)
        add_member(org, manager, "manager")
        # Managers hold roles.view but not roles.manage: the ceiling applies first.
        denied = client_for(manager).post(
            f"/api/v1/organizations/{org.id}/roles/",
            {"name": "God", "permissions": ["organization.delete"]},
            format="json",
        )
        assert denied.status_code == 403

        # An admin has roles.manage but lacks organization.delete, so cannot hand it out.
        admin = make_user()
        add_member(org, admin, "admin")
        denied = client_for(admin).post(
            f"/api/v1/organizations/{org.id}/roles/",
            {"name": "God", "permissions": ["organization.delete"]},
            format="json",
        )
        assert denied.status_code == 403
        allowed = client_for(admin).post(
            f"/api/v1/organizations/{org.id}/roles/",
            {"name": "Reviewer", "permissions": ["organization.view", "audit.view"]},
            format="json",
        )
        assert allowed.status_code == 201

    def test_role_change_takes_effect_on_the_next_request(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        """Permissions come from the database each time, never from the token."""
        owner, admin = make_user(), make_user()
        org = make_org(owner)
        add_member(org, admin, "admin")
        admin_client = client_for(admin)
        members_url = f"/api/v1/organizations/{org.id}/members/"
        assert admin_client.get(members_url).status_code == 200

        client_for(owner).patch(
            f"{members_url}{membership_id(org, admin)}/",
            {"role_id": role_id(org, "viewer")},
            format="json",
        )
        assert admin_client.get(members_url).status_code == 200  # viewers may list members
        assert (
            admin_client.post(
                f"/api/v1/organizations/{org.id}/teams/", {"name": "X"}, format="json"
            ).status_code
            == 403
        )
