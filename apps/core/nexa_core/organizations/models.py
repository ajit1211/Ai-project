"""Tenancy and role-based access control.

    User ─ OrganizationMembership ─ Role ─ RolePermission ─ Permission
                 │
                 └─ TeamMembership ─ Team

Every table that belongs to an organization carries `organization_id`. Composite foreign keys
(added in migration 0002) make the database itself refuse a membership that points at another
organization's role, or a team membership that mixes two organizations.
"""

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from nexa_common.ids import new_id


class OrganizationQuerySet(models.QuerySet["Organization"]):
    def active(self) -> "OrganizationQuerySet":
        return self.filter(deleted_at__isnull=True)


class Organization(models.Model):
    class Kind(models.TextChoices):
        PERSONAL = "personal", "Personal workspace"
        TEAM = "team", "Team organization"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(
        null=True, blank=True, help_text="Soft delete: hidden at once, purged after 30 days."
    )

    objects = OrganizationQuerySet.as_manager()

    class Meta:
        db_table = "organizations"

    def __str__(self) -> str:
        return self.name

    @property
    def is_personal(self) -> bool:
        return self.kind == self.Kind.PERSONAL


class Permission(models.Model):
    """A capability such as `team.create` or `ai.web_search`. Rows mirror `access.catalog`."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    codename = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=40)
    description = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "permissions"
        ordering = ("codename",)

    def __str__(self) -> str:
        return self.codename


class Role(models.Model):
    """A named set of permissions inside one organization.

    System roles (owner, admin, manager, member, viewer) are created for every organization
    from templates in code and cannot be edited; custom roles can.
    """

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    key = models.SlugField(max_length=50)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240, blank=True)
    is_system = models.BooleanField(default=False)
    permissions = models.ManyToManyField(Permission, through="RolePermission", related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "roles"
        constraints = [
            models.UniqueConstraint(fields=["organization", "key"], name="roles_org_key_uniq"),
            models.UniqueConstraint("organization", Lower("name"), name="roles_org_name_uniq"),
            # Target of the composite foreign keys that keep memberships inside one tenant.
            models.UniqueConstraint(fields=["organization", "id"], name="roles_org_id_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization_id})"


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "role_permissions"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="role_permissions_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.role_id} -> {self.permission_id}"


class OrganizationMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    joined_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organization_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="organization_memberships_uniq"
            ),
            models.UniqueConstraint(
                fields=["organization", "id"], name="organization_memberships_org_id_uniq"
            ),
        ]
        indexes = [models.Index(fields=["user", "status"], name="org_memberships_user_idx")]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.organization_id}"


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=64)
    description = models.CharField(max_length=240, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teams"
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="teams_org_slug_uniq"),
            models.UniqueConstraint("organization", Lower("name"), name="teams_org_name_uniq"),
            models.UniqueConstraint(fields=["organization", "id"], name="teams_org_id_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class TeamMembership(models.Model):
    """Membership of an organization member in a team. A member may join several teams."""

    class TeamRole(models.TextChoices):
        LEAD = "lead"
        MEMBER = "member"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    membership = models.ForeignKey(
        OrganizationMembership, on_delete=models.CASCADE, related_name="team_memberships"
    )
    role = models.CharField(max_length=16, choices=TeamRole.choices, default=TeamRole.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "team_memberships"
        constraints = [
            models.UniqueConstraint(fields=["team", "membership"], name="team_memberships_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.membership_id} in team {self.team_id}"


class Invitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        ACCEPTED = "accepted"
        DECLINED = "declined"
        REVOKED = "revoked"
        EXPIRED = "expired"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="+")
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    sent_count = models.PositiveSmallIntegerField(default=1)
    last_sent_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invitations"
        constraints = [
            # At most one open invitation per address and organization.
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(status="pending"),
                name="invitations_one_pending_per_email",
            ),
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="invitations_email_lowercase_chk"
            ),
        ]
        indexes = [models.Index(fields=["email", "status"], name="invitations_email_idx")]

    def __str__(self) -> str:
        return f"Invitation for {self.email} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()
