"""SQLAlchemy Core definitions of the Core-owned tables Runtime reads for authentication."""

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, Uuid

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("email", String, nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("email_verified_at", DateTime(timezone=True)),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("active_organization_id", Uuid),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)

organizations = Table(
    "organizations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String, nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
)

organization_memberships = Table(
    "organization_memberships",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("organization_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("role_id", Uuid, nullable=False),
    Column("status", String, nullable=False),
)

roles = Table(
    "roles",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", String, nullable=False),
)

role_permissions = Table(
    "role_permissions",
    metadata,
    Column("role_id", Uuid, nullable=False),
    Column("permission_id", Uuid, nullable=False),
)

permissions = Table(
    "permissions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("codename", String, nullable=False),
)

team_memberships = Table(
    "team_memberships",
    metadata,
    Column("team_id", Uuid, nullable=False),
    Column("membership_id", Uuid, nullable=False),
    Column("role", String, nullable=False),
)
