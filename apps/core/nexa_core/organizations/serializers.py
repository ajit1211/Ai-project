"""Request validation and response shapes for organizations, members, teams and roles."""

from typing import Any

from rest_framework import serializers

from nexa_core.organizations.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    Role,
    Team,
    TeamMembership,
)


class CreateOrganizationInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=120)


class UpdateOrganizationInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=120, required=False)
    settings = serializers.DictField(required=False)


class InviteInput(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    role_id = serializers.UUIDField()


class InvitationTokenInput(serializers.Serializer[dict[str, Any]]):
    token = serializers.CharField(max_length=256)


class ChangeRoleInput(serializers.Serializer[dict[str, Any]]):
    role_id = serializers.UUIDField()


class MemberStatusInput(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=OrganizationMembership.Status.choices)


class TeamInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=80)
    description = serializers.CharField(
        max_length=240, allow_blank=True, required=False, default=""
    )


class TeamUpdateInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=80, required=False)
    description = serializers.CharField(max_length=240, allow_blank=True, required=False)


class TeamMemberInput(serializers.Serializer[dict[str, Any]]):
    membership_id = serializers.UUIDField()
    role = serializers.ChoiceField(
        choices=TeamMembership.TeamRole.choices, default=TeamMembership.TeamRole.MEMBER
    )


class RoleInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=80)
    description = serializers.CharField(
        max_length=240, allow_blank=True, required=False, default=""
    )
    permissions = serializers.ListField(child=serializers.CharField(max_length=100))


class RoleUpdateInput(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=80, required=False)
    description = serializers.CharField(max_length=240, allow_blank=True, required=False)
    permissions = serializers.ListField(child=serializers.CharField(max_length=100), required=False)


def organization_payload(
    organization: Organization, membership: OrganizationMembership | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(organization.id),
        "name": organization.name,
        "slug": organization.slug,
        "kind": organization.kind,
        "created_at": organization.created_at.isoformat(),
    }
    if membership is not None:
        data["role"] = {
            "id": str(membership.role_id),
            "key": membership.role.key,
            "name": membership.role.name,
        }
    return data


def organization_detail_payload(
    organization: Organization, membership: OrganizationMembership, permissions: frozenset[str]
) -> dict[str, Any]:
    data = organization_payload(organization, membership)
    data["settings"] = organization.settings
    data["permissions"] = sorted(permissions)
    return data


def member_payload(membership: OrganizationMembership) -> dict[str, Any]:
    return {
        "id": str(membership.id),
        "user": {
            "id": str(membership.user_id),
            "email": membership.user.email,
            "full_name": membership.user.full_name,
        },
        "role": {
            "id": str(membership.role_id),
            "key": membership.role.key,
            "name": membership.role.name,
        },
        "status": membership.status,
        "joined_at": membership.joined_at.isoformat(),
    }


def invitation_payload(invitation: Invitation) -> dict[str, Any]:
    return {
        "id": str(invitation.id),
        "email": invitation.email,
        "role": {
            "id": str(invitation.role_id),
            "key": invitation.role.key,
            "name": invitation.role.name,
        },
        "status": invitation.status,
        "invited_by": str(invitation.invited_by_id) if invitation.invited_by_id else None,
        "expires_at": invitation.expires_at.isoformat(),
        "sent_count": invitation.sent_count,
        "created_at": invitation.created_at.isoformat(),
    }


def team_payload(team: Team, member_count: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(team.id),
        "name": team.name,
        "slug": team.slug,
        "description": team.description,
        "created_at": team.created_at.isoformat(),
    }
    if member_count is not None:
        data["member_count"] = member_count
    return data


def team_member_payload(team_membership: TeamMembership) -> dict[str, Any]:
    membership = team_membership.membership
    return {
        "membership_id": str(membership.id),
        "user": {
            "id": str(membership.user_id),
            "email": membership.user.email,
            "full_name": membership.user.full_name,
        },
        "role": team_membership.role,
        "added_at": team_membership.created_at.isoformat(),
    }


def role_payload(role: Role, permissions: list[str]) -> dict[str, Any]:
    return {
        "id": str(role.id),
        "key": role.key,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
        "permissions": permissions,
    }
