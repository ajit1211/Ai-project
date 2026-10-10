"""Organization, member, invitation, team and role endpoints.

Every `/organizations/{organization_id}/...` view extends `OrganizationScopedView`, which
verifies membership (404 otherwise) and the per-method permission before the handler runs.
Services then apply the finer rules (escalation, last owner, resource access).
"""

from collections import defaultdict
from typing import Any
from uuid import UUID

from django.db.models import Count
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nexa_common.access import authorize
from nexa_core.access.drf import AuthenticatedView, OrganizationScopedView, current_user
from nexa_core.api import ok
from nexa_core.organizations import invitations, role_admin, services, teams
from nexa_core.organizations.models import (
    Invitation,
    OrganizationMembership,
    Role,
    RolePermission,
    Team,
    TeamMembership,
)
from nexa_core.organizations.serializers import (
    ChangeRoleInput,
    CreateOrganizationInput,
    InvitationTokenInput,
    InviteInput,
    MemberStatusInput,
    RoleInput,
    RoleUpdateInput,
    TeamInput,
    TeamMemberInput,
    TeamUpdateInput,
    UpdateOrganizationInput,
    invitation_payload,
    member_payload,
    organization_detail_payload,
    organization_payload,
    role_payload,
    team_member_payload,
    team_payload,
)


def _validated(serializer_class: type[serializers.Serializer[Any]], data: Any) -> dict[str, Any]:
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


# --- organizations -----------------------------------------------------------------------


class OrganizationListView(AuthenticatedView):
    def get(self, request: Request) -> Response:
        memberships = (
            OrganizationMembership.objects.select_related("organization", "role")
            .filter(
                user=current_user(request),
                status=OrganizationMembership.Status.ACTIVE,
                organization__deleted_at__isnull=True,
            )
            .order_by("organization__kind", "joined_at")
        )
        return ok([organization_payload(m.organization, m) for m in memberships])

    def post(self, request: Request) -> Response:
        name = _validated(CreateOrganizationInput, request.data)["name"]
        organization = services.create_organization(name=name, owner=current_user(request))
        membership = OrganizationMembership.objects.select_related("role").get(
            organization=organization, user=current_user(request)
        )
        return ok(
            organization_payload(organization, membership),
            message="Organization created.",
            status=201,
        )


class OrganizationDetailView(OrganizationScopedView):
    required_permissions = {
        "GET": "organization.view",
        "PATCH": "organization.update",
        "DELETE": "organization.delete",
    }

    def get(self, request: Request, organization_id: UUID) -> Response:
        return ok(
            organization_detail_payload(
                self.organization, self.membership, self.principal.permissions
            )
        )

    def patch(self, request: Request, organization_id: UUID) -> Response:
        data = _validated(UpdateOrganizationInput, request.data)
        organization = services.update_organization(
            self.context, name=data.get("name"), settings=data.get("settings")
        )
        return ok(
            organization_detail_payload(organization, self.membership, self.principal.permissions),
            message="Organization updated.",
        )

    def delete(self, request: Request, organization_id: UUID) -> Response:
        services.delete_organization(self.context)
        return ok(message="Organization deleted.")


class LeaveOrganizationView(OrganizationScopedView):
    def post(self, request: Request, organization_id: UUID) -> Response:
        services.leave_organization(self.context)
        return ok(message="You have left the organization.")


# --- members -----------------------------------------------------------------------------


class MemberListView(OrganizationScopedView):
    required_permissions = {"GET": "members.view"}

    def get(self, request: Request, organization_id: UUID) -> Response:
        members = (
            OrganizationMembership.objects.select_related("user", "role")
            .filter(organization=self.organization)
            .order_by("joined_at")
        )
        return ok([member_payload(m) for m in members])


class MemberDetailView(OrganizationScopedView):
    required_permissions = {"PATCH": "members.update", "DELETE": "members.remove"}

    def patch(self, request: Request, organization_id: UUID, membership_id: UUID) -> Response:
        if "role_id" in request.data:
            role_id = _validated(ChangeRoleInput, request.data)["role_id"]
            membership = services.change_member_role(self.context, membership_id, role_id)
        else:
            status = _validated(MemberStatusInput, request.data)["status"]
            membership = services.set_member_status(self.context, membership_id, status)
        return ok(member_payload(membership), message="Member updated.")

    def delete(self, request: Request, organization_id: UUID, membership_id: UUID) -> Response:
        services.remove_member(self.context, membership_id)
        return ok(message="Member removed.")


# --- invitations -------------------------------------------------------------------------


class InvitationListView(OrganizationScopedView):
    required_permissions = {"GET": "members.invite", "POST": "members.invite"}

    def get(self, request: Request, organization_id: UUID) -> Response:
        pending = (
            Invitation.objects.select_related("role")
            .filter(organization=self.organization, status=Invitation.Status.PENDING)
            .order_by("-created_at")
        )
        return ok([invitation_payload(i) for i in pending])

    def post(self, request: Request, organization_id: UUID) -> Response:
        data = _validated(InviteInput, request.data)
        invitation = invitations.create_invitation(
            self.context,
            email=data["email"],
            role_id=data["role_id"],
            inviter=current_user(request),
        )
        return ok(invitation_payload(invitation), message="Invitation sent.", status=201)


class InvitationDetailView(OrganizationScopedView):
    required_permissions = {"DELETE": "members.invite", "POST": "members.invite"}

    def post(self, request: Request, organization_id: UUID, invitation_id: UUID) -> Response:
        """Resend."""
        invitation = invitations.resend_invitation(
            self.context, invitation_id, current_user(request)
        )
        return ok(invitation_payload(invitation), message="Invitation sent again.")

    def delete(self, request: Request, organization_id: UUID, invitation_id: UUID) -> Response:
        invitations.revoke_invitation(self.context, invitation_id)
        return ok(message="Invitation revoked.")


class InvitationPreviewView(APIView):
    """What an invitation link leads to, shown before the invitee signs in or registers."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request: Request) -> Response:
        preview = invitations.preview_invitation(
            _validated(InvitationTokenInput, request.data)["token"]
        )
        return ok(
            {
                "organization_name": preview.organization_name,
                "inviter_name": preview.inviter_name,
                "role_name": preview.role_name,
                "email": preview.email,
                "expires_at": preview.expires_at.isoformat(),
            }
        )


class InvitationAcceptView(AuthenticatedView):
    def post(self, request: Request) -> Response:
        token = _validated(InvitationTokenInput, request.data)["token"]
        membership = invitations.accept_invitation(token, current_user(request))
        membership = OrganizationMembership.objects.select_related("organization", "role").get(
            id=membership.id
        )
        return ok(
            organization_payload(membership.organization, membership),
            message=f"You joined {membership.organization.name}.",
        )


class InvitationDeclineView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request: Request) -> Response:
        invitations.decline_invitation(_validated(InvitationTokenInput, request.data)["token"])
        return ok(message="Invitation declined.")


# --- teams -------------------------------------------------------------------------------


class TeamListView(OrganizationScopedView):
    required_permissions = {"GET": "team.view", "POST": "team.create"}

    def get(self, request: Request, organization_id: UUID) -> Response:
        rows = (
            Team.objects.filter(organization=self.organization)
            .annotate(member_count=Count("memberships"))
            .order_by("name")
        )
        return ok([team_payload(team, team.member_count) for team in rows])

    def post(self, request: Request, organization_id: UUID) -> Response:
        data = _validated(TeamInput, request.data)
        team = teams.create_team(self.context, name=data["name"], description=data["description"])
        return ok(team_payload(team, 1), message="Team created.", status=201)


class TeamDetailView(OrganizationScopedView):
    required_permissions = {"GET": "team.view", "PATCH": "team.update", "DELETE": "team.delete"}

    def get(self, request: Request, organization_id: UUID, team_id: UUID) -> Response:
        team = teams.get_team(self.context, team_id)
        authorize(self.principal, "view", teams.team_resource(self.context, team))
        members = (
            TeamMembership.objects.select_related("membership__user")
            .filter(team=team)
            .order_by("created_at")
        )
        data = team_payload(team, len(members))
        data["members"] = [team_member_payload(m) for m in members]
        return ok(data)

    def patch(self, request: Request, organization_id: UUID, team_id: UUID) -> Response:
        data = _validated(TeamUpdateInput, request.data)
        team = teams.update_team(
            self.context, team_id, name=data.get("name"), description=data.get("description")
        )
        return ok(team_payload(team), message="Team updated.")

    def delete(self, request: Request, organization_id: UUID, team_id: UUID) -> Response:
        teams.delete_team(self.context, team_id)
        return ok(message="Team deleted.")


class TeamMemberListView(OrganizationScopedView):
    required_permissions = {"POST": "team.manage_members"}

    def post(self, request: Request, organization_id: UUID, team_id: UUID) -> Response:
        data = _validated(TeamMemberInput, request.data)
        team_membership = teams.add_team_member(
            self.context, team_id, data["membership_id"], data["role"]
        )
        team_membership = TeamMembership.objects.select_related("membership__user").get(
            id=team_membership.id
        )
        return ok(team_member_payload(team_membership), message="Team member added.", status=201)


class TeamMemberDetailView(OrganizationScopedView):
    required_permissions = {"DELETE": "team.manage_members"}

    def delete(
        self, request: Request, organization_id: UUID, team_id: UUID, membership_id: UUID
    ) -> Response:
        teams.remove_team_member(self.context, team_id, membership_id)
        return ok(message="Team member removed.")


# --- roles -------------------------------------------------------------------------------


def _permissions_by_role(organization_id: UUID) -> dict[UUID, list[str]]:
    grouped: defaultdict[UUID, list[str]] = defaultdict(list)
    rows = RolePermission.objects.filter(role__organization_id=organization_id).values_list(
        "role_id", "permission__codename"
    )
    for role_id, codename in rows:
        grouped[role_id].append(codename)
    return {role_id: sorted(codes) for role_id, codes in grouped.items()}


class RoleListView(OrganizationScopedView):
    required_permissions = {"GET": "roles.view", "POST": "roles.manage"}

    def get(self, request: Request, organization_id: UUID) -> Response:
        permissions = _permissions_by_role(self.organization.id)
        roles = Role.objects.filter(organization=self.organization).order_by("-is_system", "name")
        return ok([role_payload(role, permissions.get(role.id, [])) for role in roles])

    def post(self, request: Request, organization_id: UUID) -> Response:
        data = _validated(RoleInput, request.data)
        role = role_admin.create_role(
            self.context,
            name=data["name"],
            description=data["description"],
            permissions=data["permissions"],
        )
        return ok(
            role_payload(role, sorted(data["permissions"])), message="Role created.", status=201
        )


class RoleDetailView(OrganizationScopedView):
    required_permissions = {"PATCH": "roles.manage", "DELETE": "roles.manage"}

    def patch(self, request: Request, organization_id: UUID, role_id: UUID) -> Response:
        data = _validated(RoleUpdateInput, request.data)
        role = role_admin.update_role(
            self.context,
            role_id,
            name=data.get("name"),
            description=data.get("description"),
            permissions=data.get("permissions"),
        )
        permissions = _permissions_by_role(self.organization.id).get(role.id, [])
        return ok(role_payload(role, permissions), message="Role updated.")

    def delete(self, request: Request, organization_id: UUID, role_id: UUID) -> Response:
        role_admin.delete_role(self.context, role_id)
        return ok(message="Role deleted.")
