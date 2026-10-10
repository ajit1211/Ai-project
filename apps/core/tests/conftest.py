"""Shared fixtures for Core tests."""

from typing import Any

import fakeredis
import pytest
from django.utils import timezone

from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization
from nexa_core.organizations.services import create_organization
from nexa_core.testing import PASSWORD, OrgFactory, UserFactory


@pytest.fixture
def make_user(db: None) -> UserFactory:
    counter = iter(range(1, 10_000))

    def factory(email: str | None = None, *, verified: bool = True, **extra: Any) -> User:
        user = User.objects.create_user(
            email or f"user{next(counter)}@example.com", PASSWORD, **extra
        )
        if verified:
            user.email_verified_at = timezone.now()
            user.save(update_fields=["email_verified_at"])
        return user

    return factory


@pytest.fixture
def make_org(db: None) -> OrgFactory:
    def factory(owner: User, name: str = "Acme") -> Organization:
        return create_organization(name=name, owner=owner)

    return factory


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeRedis:
    """Rate limits run against an in-memory Redis, so tests are isolated and need no server."""
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer())
    monkeypatch.setattr("nexa_core.security.ratelimit._client", lambda: client)
    monkeypatch.setattr("nexa_core.security.ratelimit._unavailable_until", 0.0)
    return client
