"""FastAPI dependencies: who is calling, and what may they do.

    @router.get("/example")
    async def example(principal: CurrentPrincipal) -> ...:
        require(principal, "ai.chat")

`CurrentUser` only proves identity and a live session. `CurrentPrincipal` additionally
loads the membership in the token's active organization and its permissions; a token with
no active organization, or a membership that no longer exists, is refused.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nexa_common.access import Principal
from nexa_common.errors import AuthRequired, ErrorCode, NexaError, PermissionDenied
from nexa_common.jwt import AccessClaims, TokenInvalid, decode_access_token
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.db import tables as t


@dataclass(frozen=True)
class AuthenticatedUser:
    claims: AccessClaims
    user_id: UUID
    email: str
    session_id: UUID


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthRequired
    return token.strip()


async def current_user(request: Request) -> AuthenticatedUser:
    token = _bearer_token(request)
    jwks: JWKSClient = request.app.state.jwks
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenInvalid from exc
    claims = decode_access_token(token, await jwks.keys_for(kid if isinstance(kid, str) else None))

    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(t.users.c.email, t.users.c.is_active, t.users.c.email_verified_at)
                .select_from(
                    t.auth_sessions.join(t.users, t.users.c.id == t.auth_sessions.c.user_id)
                )
                .where(
                    t.auth_sessions.c.id == claims.session_id,
                    t.auth_sessions.c.user_id == claims.user_id,
                    t.auth_sessions.c.revoked_at.is_(None),
                    t.auth_sessions.c.expires_at > datetime.now(UTC),
                )
            )
        ).first()
    if row is None or not row.is_active or row.email_verified_at is None:
        raise NexaError(code=ErrorCode.AUTH_SESSION_REVOKED, status=401)

    structlog.contextvars.bind_contextvars(user_id=str(claims.user_id))
    return AuthenticatedUser(claims, claims.user_id, row.email, claims.session_id)


async def load_principal(
    conn: AsyncConnection, user: AuthenticatedUser, organization_id: UUID
) -> Principal:
    """Membership, role and permissions in `organization_id`, from the database only."""
    membership = (
        await conn.execute(
            select(
                t.organization_memberships.c.id, t.organization_memberships.c.role_id, t.roles.c.key
            )
            .select_from(
                t.organization_memberships.join(
                    t.roles, t.roles.c.id == t.organization_memberships.c.role_id
                ).join(
                    t.organizations,
                    t.organizations.c.id == t.organization_memberships.c.organization_id,
                )
            )
            .where(
                t.organization_memberships.c.user_id == user.user_id,
                t.organization_memberships.c.organization_id == organization_id,
                t.organization_memberships.c.status == "active",
                t.organizations.c.deleted_at.is_(None),
            )
        )
    ).first()
    if membership is None:
        raise PermissionDenied("You are no longer a member of the active organization.")

    codenames = await conn.execute(
        select(t.permissions.c.codename)
        .select_from(
            t.role_permissions.join(
                t.permissions, t.permissions.c.id == t.role_permissions.c.permission_id
            )
        )
        .where(t.role_permissions.c.role_id == membership.role_id)
    )
    teams = await conn.execute(
        select(t.team_memberships.c.team_id, t.team_memberships.c.role).where(
            t.team_memberships.c.membership_id == membership.id
        )
    )
    team_rows = teams.all()
    return Principal(
        user_id=user.user_id,
        session_id=user.session_id,
        organization_id=organization_id,
        membership_id=membership.id,
        role_key=membership.key,
        permissions=frozenset(codenames.scalars().all()),
        team_ids=frozenset(row.team_id for row in team_rows),
        led_team_ids=frozenset(row.team_id for row in team_rows if row.role == "lead"),
    )


async def current_principal(
    request: Request, user: Annotated[AuthenticatedUser, Depends(current_user)]
) -> Principal:
    organization_id = user.claims.organization_id
    if organization_id is None:
        raise PermissionDenied("Select an organization first.")
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        principal = await load_principal(conn, user, organization_id)
    structlog.contextvars.bind_contextvars(organization_id=str(organization_id))
    return principal


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
