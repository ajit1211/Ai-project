"""Accounts, profiles, one-time tokens and sessions.

Design notes (docs/architecture/authentication.md):
- The user is identified by email; the primary key is a UUIDv7 that is safe to expose.
- `full_name` rather than first/last name: many names (including many Indian names) do not
  split into a given name and a family name.
- Preferences live on `UserProfile`, not on `User`, so the authentication table stays small
  and changes to preferences never touch security-relevant rows.
- Secrets (verification links, reset links, refresh tokens) are stored only as SHA-256 hashes.
"""

from typing import Any, ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from nexa_common.ids import new_id
from nexa_core.identity.managers import UserManager, normalize_email


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="False when the account is deactivated; it cannot sign in."
    )
    is_staff = models.BooleanField(
        default=False, help_text="Platform staff: may sign in to the Django admin."
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        db_table = "users"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="users_email_lowercase_chk"
            ),
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    def set_password(self, raw_password: str | None) -> None:
        super().set_password(raw_password)
        self.password_changed_at = timezone.now()

    def clean(self) -> None:
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)


class UserProfile(models.Model):
    """Presentation and preference data. `preferences` is validated by `UserPreferences`."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, primary_key=True, related_name="profile"
    )
    avatar_url = models.URLField(max_length=500, blank=True)
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA time zone name")
    locale = models.CharField(max_length=16, default="en", help_text="BCP 47 language tag")
    preferences = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_profiles"

    def __str__(self) -> str:
        return f"Profile of {self.user_id}"


class OneTimeToken(models.Model):
    """A single-use, expiring secret sent by email. Only its SHA-256 hash is stored."""

    class Purpose(models.TextChoices):
        EMAIL_VERIFICATION = "email_verification"
        PASSWORD_RESET = "password_reset"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="one_time_tokens")
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    email = models.EmailField(help_text="The address the link was sent to.")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "one_time_tokens"
        indexes = [
            models.Index(
                fields=["user", "purpose"],
                condition=models.Q(used_at__isnull=True),
                name="one_time_tokens_open_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.purpose} token for {self.user_id}"


class AuthSession(models.Model):
    """One sign-in on one device. Revoking it invalidates its refresh and access tokens."""

    class ClientType(models.TextChoices):
        WEB = "web"
        MOBILE = "mobile"

    class RevokedReason(models.TextChoices):
        LOGOUT = "logout"
        LOGOUT_ALL = "logout_all"
        REVOKED_BY_USER = "revoked_by_user"
        PASSWORD_CHANGED = "password_changed"
        PASSWORD_RESET = "password_reset"
        TOKEN_REUSE = "token_reuse"
        ACCOUNT_DEACTIVATED = "account_deactivated"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    client_type = models.CharField(max_length=16, choices=ClientType.choices)
    device_label = models.CharField(max_length=120, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    active_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(help_text="Absolute end of the session, refreshes or not.")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=32, choices=RevokedReason.choices, blank=True)

    class Meta:
        db_table = "auth_sessions"
        indexes = [
            models.Index(
                fields=["user", "-last_seen_at"],
                condition=models.Q(revoked_at__isnull=True),
                name="auth_sessions_user_active_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.device_label or 'Session'} ({self.user_id})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()


class RefreshToken(models.Model):
    """One link in a session's rotation chain. Each token can be exchanged exactly once."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    session = models.ForeignKey(
        AuthSession, on_delete=models.CASCADE, related_name="refresh_tokens"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "refresh_tokens"

    def __str__(self) -> str:
        return f"Refresh token in session {self.session_id}"
