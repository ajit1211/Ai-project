from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import BaseUserManager

if TYPE_CHECKING:
    from nexa_core.identity.models import User


def normalize_email(email: str) -> str:
    """Emails are stored lowercase so a plain unique index enforces case-insensitive uniqueness."""
    return email.strip().lower()


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> "User":
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=normalize_email(email), **extra)
        user.set_password(password)  # None stores an unusable password
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> "User":
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not (extra["is_staff"] and extra["is_superuser"]):
            raise ValueError("A superuser must have is_staff and is_superuser set")
        return self.create_user(email, password, **extra)

    def get_by_natural_key(self, username: str | None) -> "User":
        return self.get(email=normalize_email(username or ""))
