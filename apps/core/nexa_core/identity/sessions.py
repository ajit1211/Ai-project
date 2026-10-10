"""Sessions, refresh-token rotation and revocation (docs/architecture/authentication.md).

Lifetimes:
- access token: 10 minutes. Short enough that a leaked token is useful only briefly, long
  enough that a busy client refreshes about six times an hour.
- refresh token: 14 days of inactivity. Each use returns a new one; the old one is spent.
- session: 30 days absolute. After that the user signs in again, however active they are.

Reuse detection: refresh tokens form a chain. Presenting a token that was already exchanged
means two parties hold it, so the whole session is revoked. A 10-second grace window covers
two browser tabs refreshing at the same moment.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from django.db import transaction
from django.utils import timezone

from nexa_common.errors import ErrorCode, NexaError
from nexa_common.jwt import ACCESS_TOKEN_TTL, encode_access_token
from nexa_core import request_meta
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.identity.devices import describe_device
from nexa_core.identity.keys import signing_key
from nexa_core.identity.models import AuthSession, RefreshToken, User
from nexa_core.organizations.models import Organization, OrganizationMembership
from nexa_core.security.one_time import hash_secret, new_secret

REFRESH_TOKEN_TTL = timedelta(days=14)
SESSION_TTL = timedelta(days=30)
REUSE_GRACE = timedelta(seconds=10)
NEW_DEVICE_LOOKBACK = timedelta(days=90)


class SessionEnded(NexaError):
    status = 401
    code = ErrorCode.AUTH_SESSION_REVOKED


class RefreshConflict(NexaError):
    """The token was exchanged a moment ago, most likely by another tab. Not an attack."""

    status = 401
    code = ErrorCode.AUTH_TOKEN_INVALID


@dataclass(frozen=True)
class IssuedTokens:
    session: AuthSession
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


def _active_membership(user_id: uuid.UUID, organization_id: uuid.UUID | None) -> bool:
    if organization_id is None:
        return False
    return OrganizationMembership.objects.filter(
        user_id=user_id,
        organization_id=organization_id,
        status=OrganizationMembership.Status.ACTIVE,
        organization__deleted_at__isnull=True,
    ).exists()


def default_organization(user: User) -> Organization | None:
    """The organization a new session starts in: the personal workspace, else the oldest."""
    membership = (
        OrganizationMembership.objects.filter(
            user=user,
            status=OrganizationMembership.Status.ACTIVE,
            organization__deleted_at__isnull=True,
        )
        .select_related("organization")
        .order_by("organization__kind", "joined_at")  # "personal" sorts before "team"
        .first()
    )
    return membership.organization if membership else None


def issue_access_token(session: AuthSession) -> tuple[str, int]:
    """Mint an access token for the session's active organization, if still a member there."""
    organization_id = session.active_organization_id
    if organization_id and not _active_membership(session.user_id, organization_id):
        session.active_organization = None
        session.save(update_fields=["active_organization"])
        organization_id = None
    token = encode_access_token(
        signing_key(),
        user_id=session.user_id,
        session_id=session.id,
        organization_id=organization_id,
        token_id=uuid.uuid4().hex,
    )
    return token, int(ACCESS_TOKEN_TTL.total_seconds())


def _new_refresh_token(session: AuthSession, parent: RefreshToken | None) -> tuple[str, datetime]:
    raw = new_secret()
    expires_at = min(timezone.now() + REFRESH_TOKEN_TTL, session.expires_at)
    RefreshToken.objects.create(
        session=session, token_hash=hash_secret(raw), parent=parent, expires_at=expires_at
    )
    return raw, expires_at


def _is_new_device(user: User, device_label: str) -> bool:
    previous = AuthSession.objects.filter(
        user=user, created_at__gte=timezone.now() - NEW_DEVICE_LOOKBACK
    )
    return previous.exists() and not previous.filter(device_label=device_label).exists()


@transaction.atomic
def start_session(user: User, *, client_type: str) -> IssuedTokens:
    meta = request_meta.current()
    device_label = describe_device(meta.user_agent)
    new_device = _is_new_device(user, device_label)
    now = timezone.now()
    session = AuthSession.objects.create(
        user=user,
        client_type=client_type,
        device_label=device_label,
        user_agent=meta.user_agent,
        ip_address=meta.ip_address,
        active_organization=default_organization(user),
        last_seen_at=now,
        expires_at=now + SESSION_TTL,
    )
    refresh_token, refresh_expires_at = _new_refresh_token(session, parent=None)
    access_token, expires_in = issue_access_token(session)

    record(
        AuditAction.SESSION_CREATED,
        actor_id=user.id,
        resource_type="session",
        resource_id=session.id,
        metadata={"client_type": client_type, "device": device_label},
    )
    if new_device:
        record(AuditAction.NEW_DEVICE_LOGIN, actor_id=user.id, metadata={"device": device_label})
    return IssuedTokens(session, access_token, expires_in, refresh_token, refresh_expires_at)


class _Outcome(Enum):
    ROTATED = auto()
    UNKNOWN = auto()
    ENDED = auto()
    CONFLICT = auto()
    REUSED = auto()


def rotate_refresh_token(raw_token: str) -> IssuedTokens:
    """Exchange a refresh token for a new access token and a new refresh token."""
    now = timezone.now()
    meta = request_meta.current()
    new_refresh: tuple[str, datetime] | None = None

    # Decide and apply inside one transaction; raise only after it commits, so that a
    # revocation caused by token reuse is not rolled back by the error that reports it.
    with transaction.atomic():
        token = (
            RefreshToken.objects.select_for_update()
            .select_related("session", "session__user")
            .filter(token_hash=hash_secret(raw_token))
            .first()
        )
        if token is None:
            outcome = _Outcome.UNKNOWN
        else:
            session = token.session
            if not session.is_active or not session.user.is_active:
                outcome = _Outcome.ENDED
            elif token.used_at is not None:
                outcome = (
                    _Outcome.CONFLICT if now - token.used_at <= REUSE_GRACE else _Outcome.REUSED
                )
            elif token.expires_at <= now:
                outcome = _Outcome.ENDED
            else:
                outcome = _Outcome.ROTATED

            if outcome is _Outcome.REUSED:
                revoke_session(session, AuthSession.RevokedReason.TOKEN_REUSE)
                record(
                    AuditAction.TOKEN_REUSE_DETECTED,
                    actor_id=session.user_id,
                    resource_type="session",
                    resource_id=session.id,
                )
            elif outcome is _Outcome.ROTATED:
                token.used_at = now
                token.save(update_fields=["used_at"])
                new_refresh = _new_refresh_token(session, parent=token)
                session.last_seen_at = now
                session.ip_address = meta.ip_address or session.ip_address
                session.save(update_fields=["last_seen_at", "ip_address"])

    if outcome is _Outcome.UNKNOWN:
        raise NexaError(code=ErrorCode.AUTH_TOKEN_INVALID, status=401)
    if outcome is _Outcome.CONFLICT:
        raise RefreshConflict("This session was refreshed a moment ago. Retry the request.")
    if outcome is not _Outcome.ROTATED or token is None or new_refresh is None:
        raise SessionEnded
    access_token, expires_in = issue_access_token(token.session)
    return IssuedTokens(token.session, access_token, expires_in, *new_refresh)


def revoke_session(session: AuthSession, reason: str, *, actor_id: uuid.UUID | None = None) -> bool:
    """End one session. Its refresh tokens stop working at once; so do its access tokens,
    because every request checks the session."""
    updated = AuthSession.objects.filter(id=session.id, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason=reason
    )
    if updated:
        record(
            AuditAction.SESSION_REVOKED,
            actor_id=actor_id or session.user_id,
            resource_type="session",
            resource_id=session.id,
            metadata={"reason": reason},
        )
    return bool(updated)


def revoke_all_sessions(
    user: User, reason: str, *, except_session_id: uuid.UUID | None = None
) -> int:
    sessions = AuthSession.objects.filter(user=user, revoked_at__isnull=True)
    if except_session_id:
        sessions = sessions.exclude(id=except_session_id)
    count = sessions.update(revoked_at=timezone.now(), revoked_reason=reason)
    if count:
        record(
            AuditAction.ALL_SESSIONS_REVOKED,
            actor_id=user.id,
            metadata={"reason": reason, "sessions": count},
        )
    return count


def switch_organization(session: AuthSession, organization_id: uuid.UUID) -> tuple[str, int]:
    """Point the session at another organization the user belongs to, and mint a token for it."""
    if not _active_membership(session.user_id, organization_id):
        # Same answer whether the organization exists or not.
        raise NexaError(code=ErrorCode.RESOURCE_NOT_FOUND, status=404)
    session.active_organization_id = organization_id
    session.save(update_fields=["active_organization"])
    return issue_access_token(session)
