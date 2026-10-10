"""Account workflows: registration, verification, login, passwords, profile, deactivation.

Responses that could reveal whether an email is registered (register, resend verification,
forgot password) behave identically for known and unknown addresses, including in timing.
"""

import hashlib
from datetime import timedelta
from typing import Any

from django.contrib.auth import authenticate
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from nexa_common.errors import ErrorCode, FieldError, NexaError, ValidationFailed
from nexa_core import request_meta
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.identity import emails
from nexa_core.identity.managers import normalize_email
from nexa_core.identity.models import AuthSession, OneTimeToken, User, UserProfile
from nexa_core.identity.preferences import UserPreferences
from nexa_core.identity.sessions import IssuedTokens, revoke_all_sessions, start_session
from nexa_core.organizations.models import Organization, OrganizationMembership
from nexa_core.organizations.services import create_personal_organization
from nexa_core.security import ratelimit
from nexa_core.security.one_time import hash_secret, new_secret

VERIFICATION_TTL = timedelta(hours=24)
PASSWORD_RESET_TTL = timedelta(minutes=30)


class InvalidCredentials(NexaError):
    status = 401
    code = ErrorCode.AUTH_INVALID_CREDENTIALS


def _ip() -> str:
    return request_meta.current().ip_address or "unknown"


def _email_fingerprint(email: str) -> str:
    """Correlates failed logins for one address without storing the address itself."""
    return hashlib.sha256(email.encode()).hexdigest()[:16]


def _check_password_strength(password: str, user: User) -> None:
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise ValidationFailed(
            details=[
                FieldError(
                    field="password",
                    code=error.code or "invalid",
                    message=str(error.message % (error.params or {})),
                )
                for error in exc.error_list
            ]
        ) from exc


# --- one-time tokens ---------------------------------------------------------------------


def _issue_one_time_token(user: User, purpose: str, ttl: timedelta) -> str:
    """Create a fresh token and invalidate any still-open token for the same purpose."""
    now = timezone.now()
    OneTimeToken.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(
        used_at=now
    )
    raw = new_secret()
    OneTimeToken.objects.create(
        user=user,
        purpose=purpose,
        token_hash=hash_secret(raw),
        email=user.email,
        expires_at=now + ttl,
    )
    return raw


def _redeem_one_time_token(raw: str, purpose: str) -> OneTimeToken:
    """Spend a token exactly once. Locking the row means two simultaneous clicks can't both win."""
    ratelimit.consume(ratelimit.TOKEN_REDEEM_PER_IP, _ip())
    token = (
        OneTimeToken.objects.select_for_update()
        .select_related("user")
        .filter(token_hash=hash_secret(raw), purpose=purpose)
        .first()
    )
    # A link sent to an old address must not verify or reset after the email changed.
    if token is None or token.used_at is not None or token.email != token.user.email:
        raise NexaError(code=ErrorCode.TOKEN_INVALID, status=400)
    if token.expires_at <= timezone.now():
        raise NexaError(code=ErrorCode.TOKEN_EXPIRED, status=400)
    token.used_at = timezone.now()
    token.save(update_fields=["used_at"])
    return token


# --- registration and verification -------------------------------------------------------


def register(*, email: str, password: str, full_name: str) -> None:
    ratelimit.consume(ratelimit.REGISTER_PER_IP, _ip())
    email = normalize_email(email)
    _check_password_strength(password, User(email=email, full_name=full_name))

    existing = User.objects.filter(email=email).first()
    if existing is not None:
        make_password(password)  # spend the same time as a real signup
        if existing.is_verified:
            emails.account_exists_email(email)
        else:
            _send_verification(existing)
        return

    with transaction.atomic():
        user = User.objects.create_user(email, password, full_name=full_name.strip())
        create_personal_organization(user)
        raw = _issue_one_time_token(user, OneTimeToken.Purpose.EMAIL_VERIFICATION, VERIFICATION_TTL)
        record(
            AuditAction.USER_REGISTERED, actor_id=user.id, resource_type="user", resource_id=user.id
        )
        emails.verification_email(user.email, user.full_name, raw)


def _send_verification(user: User) -> None:
    raw = _issue_one_time_token(user, OneTimeToken.Purpose.EMAIL_VERIFICATION, VERIFICATION_TTL)
    emails.verification_email(user.email, user.full_name, raw)


@transaction.atomic
def verify_email(raw_token: str) -> User:
    token = _redeem_one_time_token(raw_token, OneTimeToken.Purpose.EMAIL_VERIFICATION)
    user = token.user
    if not user.is_verified:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
        record(
            AuditAction.EMAIL_VERIFIED, actor_id=user.id, resource_type="user", resource_id=user.id
        )
    return user


@transaction.atomic
def resend_verification(email: str) -> None:
    ratelimit.consume(ratelimit.VERIFICATION_RESEND_PER_IP, _ip())
    email = normalize_email(email)
    user = User.objects.filter(email=email, is_active=True, email_verified_at__isnull=True).first()
    if user is None:
        return
    try:
        ratelimit.consume(ratelimit.VERIFICATION_RESEND_PER_ACCOUNT, email)
    except NexaError:
        return  # silently: an error here would reveal that the account exists
    _send_verification(user)


# --- login -------------------------------------------------------------------------------


def login(*, email: str, password: str, client_type: str) -> IssuedTokens:
    email = normalize_email(email)
    ratelimit.consume(ratelimit.LOGIN_PER_IP, _ip())
    # Temporary per-account lock after repeated failures (15 minutes, never permanent).
    ratelimit.ensure_below(ratelimit.LOGIN_FAILURES_PER_ACCOUNT, email)

    user = authenticate(email=email, password=password)  # constant-time for unknown emails
    if not isinstance(user, User):
        ratelimit.hit(ratelimit.LOGIN_FAILURES_PER_ACCOUNT, email)
        known = User.objects.filter(email=email).values_list("id", flat=True).first()
        record(
            AuditAction.USER_LOGIN_FAILED,
            actor_id=known,
            metadata={"email_fingerprint": _email_fingerprint(email), "known_account": bool(known)},
        )
        raise InvalidCredentials
    if not user.is_verified:
        # Revealed only to someone who knows the password.
        raise NexaError(code=ErrorCode.EMAIL_NOT_VERIFIED, status=403)

    ratelimit.reset(ratelimit.LOGIN_FAILURES_PER_ACCOUNT, email)
    issued = start_session(user, client_type=client_type)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    record(AuditAction.USER_LOGIN, actor_id=user.id, metadata={"client_type": client_type})
    return issued


# --- passwords ---------------------------------------------------------------------------


@transaction.atomic
def request_password_reset(email: str) -> None:
    ratelimit.consume(ratelimit.PASSWORD_RESET_PER_IP, _ip())
    email = normalize_email(email)
    user = User.objects.filter(email=email, is_active=True).first()
    if user is None:
        return
    try:
        ratelimit.consume(ratelimit.PASSWORD_RESET_PER_ACCOUNT, email)
    except NexaError:
        return  # silently, for the same reason as resend_verification
    raw = _issue_one_time_token(user, OneTimeToken.Purpose.PASSWORD_RESET, PASSWORD_RESET_TTL)
    record(AuditAction.PASSWORD_RESET_REQUESTED, actor_id=user.id)
    emails.password_reset_email(user.email, raw)


@transaction.atomic
def reset_password(raw_token: str, new_password: str) -> None:
    token = _redeem_one_time_token(raw_token, OneTimeToken.Purpose.PASSWORD_RESET)
    user = token.user
    if not user.is_active:
        raise NexaError(code=ErrorCode.TOKEN_INVALID, status=400)
    _check_password_strength(new_password, user)

    user.set_password(new_password)
    # The reset link went to this address, so following it proves the user owns it.
    user.email_verified_at = user.email_verified_at or timezone.now()
    user.save(update_fields=["password", "password_changed_at", "email_verified_at"])
    OneTimeToken.objects.filter(
        user=user, purpose=OneTimeToken.Purpose.PASSWORD_RESET, used_at__isnull=True
    ).update(used_at=timezone.now())
    revoke_all_sessions(user, AuthSession.RevokedReason.PASSWORD_RESET)
    ratelimit.reset(ratelimit.LOGIN_FAILURES_PER_ACCOUNT, user.email)
    record(AuditAction.PASSWORD_RESET, actor_id=user.id)
    emails.password_changed_email(user.email)


@transaction.atomic
def change_password(
    user: User, *, current_password: str, new_password: str, session: AuthSession
) -> None:
    if not user.check_password(current_password):
        ratelimit.hit(ratelimit.LOGIN_FAILURES_PER_ACCOUNT, user.email)
        raise ValidationFailed(
            details=[
                FieldError(
                    field="current_password",
                    code="incorrect",
                    message="The current password is incorrect.",
                )
            ]
        )
    _check_password_strength(new_password, user)
    user.set_password(new_password)
    user.save(update_fields=["password", "password_changed_at"])
    revoke_all_sessions(
        user, AuthSession.RevokedReason.PASSWORD_CHANGED, except_session_id=session.id
    )
    record(AuditAction.PASSWORD_CHANGED, actor_id=user.id)
    emails.password_changed_email(user.email)


# --- profile and account -----------------------------------------------------------------


@transaction.atomic
def update_profile(user: User, changes: dict[str, Any]) -> User:
    profile = UserProfile.objects.select_for_update().get(user=user)
    if "full_name" in changes:
        user.full_name = changes["full_name"].strip()
        user.save(update_fields=["full_name", "updated_at"])
    for field in ("avatar_url", "timezone", "locale"):
        if field in changes:
            setattr(profile, field, changes[field])
    if "preferences" in changes:
        merged = UserPreferences.model_validate(profile.preferences).model_dump()
        for key, value in changes["preferences"].items():
            merged[key] = {**merged.get(key, {}), **value} if isinstance(value, dict) else value
        try:
            profile.preferences = UserPreferences.model_validate(merged).model_dump()
        except PydanticValidationError as exc:
            raise ValidationFailed(
                details=[
                    FieldError(
                        field="preferences." + ".".join(str(part) for part in error["loc"]),
                        code=error["type"],
                        message=error["msg"],
                    )
                    for error in exc.errors()
                ]
            ) from exc
    profile.save()
    record(AuditAction.PROFILE_UPDATED, actor_id=user.id, metadata={"fields": sorted(changes)})
    return user


def _sole_owner_of_shared_organization(user: User) -> bool:
    """True if leaving would orphan a team organization that has other members."""
    owned = Organization.objects.active().filter(
        kind=Organization.Kind.TEAM,
        memberships__user=user,
        memberships__role__key="owner",
        memberships__status=OrganizationMembership.Status.ACTIVE,
    )
    for organization in owned:
        others = organization.memberships.exclude(user=user)
        other_owners = others.filter(role__key="owner", status=OrganizationMembership.Status.ACTIVE)
        if others.exists() and not other_owners.exists():
            return True
    return False


@transaction.atomic
def deactivate_account(user: User, password: str) -> None:
    if not user.check_password(password):
        raise ValidationFailed(
            details=[
                FieldError(field="password", code="incorrect", message="The password is incorrect.")
            ]
        )
    if _sole_owner_of_shared_organization(user):
        raise NexaError(
            "Transfer ownership of your organizations before deactivating your account.",
            code=ErrorCode.CONFLICT,
            status=409,
        )
    user.is_active = False
    user.deactivated_at = timezone.now()
    user.save(update_fields=["is_active", "deactivated_at"])
    revoke_all_sessions(user, AuthSession.RevokedReason.ACCOUNT_DEACTIVATED)
    record(
        AuditAction.USER_DEACTIVATED, actor_id=user.id, resource_type="user", resource_id=user.id
    )
