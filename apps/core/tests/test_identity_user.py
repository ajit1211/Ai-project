import pytest
from django.db import IntegrityError

from nexa_core.identity.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def test_create_user_normalizes_email_and_hashes_password() -> None:
    user = User.objects.create_user("  Ada@Example.COM ", PASSWORD)
    assert user.email == "ada@example.com"
    assert user.check_password(PASSWORD)
    assert user.password.startswith("argon2")
    assert user.id.version == 7


def test_email_is_unique_regardless_of_case() -> None:
    User.objects.create_user("ada@example.com")
    with pytest.raises(IntegrityError):
        User.objects.create_user("ADA@example.com")


def test_login_lookup_ignores_case() -> None:
    User.objects.create_user("ada@example.com", PASSWORD)
    assert User.objects.get_by_natural_key("Ada@Example.com").email == "ada@example.com"


def test_user_without_password_cannot_log_in() -> None:
    user = User.objects.create_user("ada@example.com")
    assert not user.has_usable_password()


def test_create_superuser_sets_staff_flags() -> None:
    admin = User.objects.create_superuser("root@example.com", PASSWORD)
    assert admin.is_staff
    assert admin.is_superuser
