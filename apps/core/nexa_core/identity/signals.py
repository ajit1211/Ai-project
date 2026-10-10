from typing import Any

from nexa_core.identity.models import User, UserProfile


def create_profile(sender: type[User], instance: User, created: bool, **_: Any) -> None:
    """Every user, however created (signup, admin, createsuperuser), has a profile."""
    if created:
        UserProfile.objects.get_or_create(user=instance)
