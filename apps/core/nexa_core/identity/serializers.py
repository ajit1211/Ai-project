"""Request validation and response shapes for accounts and sessions.

Responses are built from explicit field lists: nothing reaches a client because it happens
to be on a model. Password hashes, token hashes, staff flags and internal timestamps are
never serialized.
"""

from typing import Any
from zoneinfo import available_timezones

from django.utils import timezone
from rest_framework import serializers

from nexa_core.identity.models import AuthSession, User

CLIENT_TYPES = [AuthSession.ClientType.WEB, AuthSession.ClientType.MOBILE]


class RegisterInput(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    full_name = serializers.CharField(max_length=150, allow_blank=True, required=False, default="")


class LoginInput(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    client = serializers.ChoiceField(choices=CLIENT_TYPES, default=AuthSession.ClientType.WEB)


class RefreshInput(serializers.Serializer[dict[str, Any]]):
    refresh_token = serializers.CharField(max_length=256, required=False)


class TokenInput(serializers.Serializer[dict[str, Any]]):
    token = serializers.CharField(max_length=256)


class EmailInput(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)


class ResetPasswordInput(serializers.Serializer[dict[str, Any]]):
    token = serializers.CharField(max_length=256)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


class ChangePasswordInput(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)


class PasswordInput(serializers.Serializer[dict[str, Any]]):
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class SwitchOrganizationInput(serializers.Serializer[dict[str, Any]]):
    organization_id = serializers.UUIDField()


class ProfileUpdateInput(serializers.Serializer[dict[str, Any]]):
    full_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    avatar_url = serializers.URLField(max_length=500, allow_blank=True, required=False)
    timezone = serializers.CharField(max_length=64, required=False)
    locale = serializers.RegexField(
        r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$", max_length=16, required=False
    )
    preferences = serializers.DictField(required=False)

    def validate_timezone(self, value: str) -> str:
        if value not in available_timezones():
            raise serializers.ValidationError("Unknown time zone.")
        return value

    def validate_avatar_url(self, value: str) -> str:
        if value and not value.startswith("https://"):
            raise serializers.ValidationError("Avatar URLs must use https.")
        return value


def user_payload(user: User) -> dict[str, Any]:
    profile = user.profile
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "is_verified": user.is_verified,
        "created_at": user.created_at.isoformat(),
        "profile": {
            "avatar_url": profile.avatar_url,
            "timezone": profile.timezone,
            "locale": profile.locale,
            "preferences": profile.preferences,
        },
    }


def session_payload(session: AuthSession, current_session_id: Any) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "device": session.device_label or "Unknown device",
        "client_type": session.client_type,
        "ip_address": session.ip_address,
        "created_at": session.created_at.isoformat(),
        "last_seen_at": session.last_seen_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "is_current": session.id == current_session_id,
    }


def token_payload(access_token: str, expires_in: int, session: AuthSession) -> dict[str, Any]:
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "session_id": str(session.id),
        "active_organization_id": (
            str(session.active_organization_id) if session.active_organization_id else None
        ),
        "issued_at": timezone.now().isoformat(),
    }
