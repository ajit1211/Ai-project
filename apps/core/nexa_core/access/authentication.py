"""DRF authentication: a Bearer access token, checked against a live session on every request.

The signature proves Core issued the token; the session lookup makes logout, "sign out
everywhere", password changes and token-reuse revocation take effect immediately rather than
when the 10-minute token expires.
"""

from dataclasses import dataclass

import structlog
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from nexa_common.jwt import AccessClaims, TokenInvalid, decode_access_token
from nexa_core.identity.keys import verification_keys
from nexa_core.identity.models import AuthSession, User
from nexa_core.identity.sessions import SessionEnded

WWW_AUTHENTICATE = 'Bearer realm="nexa"'


@dataclass(frozen=True)
class AuthContext:
    claims: AccessClaims
    session: AuthSession


class JWTAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[User, AuthContext] | None:
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"bearer":
            return None  # no credential: DRF's permission checks answer 401
        if len(parts) != 2:
            raise TokenInvalid

        claims = decode_access_token(parts[1].decode("latin-1"), verification_keys())
        session = (
            AuthSession.objects.select_related("user")
            .filter(
                id=claims.session_id,
                user_id=claims.user_id,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if session is None or not session.user.is_active or not session.user.is_verified:
            raise SessionEnded

        structlog.contextvars.bind_contextvars(user_id=str(session.user_id))
        return session.user, AuthContext(claims=claims, session=session)

    def authenticate_header(self, request: Request) -> str:
        return WWW_AUTHENTICATE
