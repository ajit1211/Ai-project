"""Runtime accepts Core-issued tokens and loads authorization from the shared database.

These tests commit real rows (`transaction=True`) because Runtime reads them over its own
database connection, outside Django's test transaction.
"""

from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.db import connection
from django.utils import timezone
from fastapi.testclient import TestClient

from nexa_common.jwt import encode_access_token
from nexa_core.identity.keys import jwks, signing_key
from nexa_core.identity.models import AuthSession, User
from nexa_core.identity.sessions import revoke_session, start_session
from nexa_core.organizations.models import OrganizationMembership, Role
from nexa_core.organizations.services import create_organization
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app

pytestmark = pytest.mark.django_db(transaction=True)


def _test_database_url() -> str:
    """Runtime connects to the same test database Django is using."""
    d = connection.settings_dict
    return (
        f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
        f"{d['PORT'] or 5432}/{d['NAME']}"
    )


@pytest.fixture
def runtime() -> Iterator[TestClient]:
    # Core's real JWKS document, served through an in-memory transport instead of HTTP.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    settings = RuntimeSettings(
        nexa_env="test",
        runtime_database_url=_test_database_url(),
        redis_queue_url="redis://127.0.0.1:1/0",
    )
    app = create_app(settings, jwks_client=JWKSClient(settings.core_jwks_url, transport=transport))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def owner() -> User:
    user = User.objects.create_user("owner@example.com", "correct-horse-battery-staple")
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    create_organization(name="Acme", owner=user)
    return user


def _bearer(user: User) -> dict[str, str]:
    issued = start_session(user, client_type="web")
    return {"Authorization": f"Bearer {issued.access_token}"}


def test_whoami_reflects_verified_identity_and_database_permissions(
    runtime: TestClient, owner: User
) -> None:
    response = runtime.get("/api/v1/runtime/whoami", headers=_bearer(owner))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["email"] == "owner@example.com"
    assert data["role"] == "owner"
    assert "organization.delete" in data["permissions"]


def test_requests_without_a_token_are_refused(runtime: TestClient) -> None:
    response = runtime.get("/api/v1/runtime/whoami")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_garbage_and_foreign_tokens_are_refused(runtime: TestClient, owner: User) -> None:
    garbage = runtime.get("/api/v1/runtime/whoami", headers={"Authorization": "Bearer nope"})
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "AUTH_TOKEN_INVALID"

    # Signed by a key Core never published: Runtime refetches the JWKS, then still refuses.
    session = AuthSession.objects.create(
        user=owner, client_type="web", expires_at=timezone.now() + timedelta(days=1)
    )
    forged = encode_access_token(
        Ed25519PrivateKey.generate(),
        user_id=owner.id,
        session_id=session.id,
        organization_id=None,
        token_id="x",
    )
    response = runtime.get("/api/v1/runtime/whoami", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_INVALID"


def test_revoked_session_is_refused_at_once(runtime: TestClient, owner: User) -> None:
    headers = _bearer(owner)
    assert runtime.get("/api/v1/runtime/whoami", headers=headers).status_code == 200
    revoke_session(AuthSession.objects.get(user=owner), AuthSession.RevokedReason.LOGOUT)
    response = runtime.get("/api/v1/runtime/whoami", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_SESSION_REVOKED"


def test_role_changes_apply_on_the_next_request(runtime: TestClient, owner: User) -> None:
    headers = _bearer(owner)
    membership = OrganizationMembership.objects.get(user=owner)
    membership.role = Role.objects.get(organization=membership.organization, key="viewer")
    membership.save()
    data = runtime.get("/api/v1/runtime/whoami", headers=headers).json()["data"]
    assert data["role"] == "viewer"
    assert "organization.delete" not in data["permissions"]


def test_suspended_member_loses_the_organization(runtime: TestClient, owner: User) -> None:
    headers = _bearer(owner)
    OrganizationMembership.objects.filter(user=owner).update(status="suspended")
    response = runtime.get("/api/v1/runtime/whoami", headers=headers)
    assert response.status_code == 403


def test_token_for_an_organization_the_user_left(runtime: TestClient, owner: User) -> None:
    """A token whose org claim no longer matches a membership is refused, not trusted."""
    session = AuthSession.objects.create(
        user=owner, client_type="web", expires_at=timezone.now() + timedelta(days=1)
    )
    token = encode_access_token(
        signing_key(),
        user_id=owner.id,
        session_id=session.id,
        organization_id=uuid4(),  # not an organization the user belongs to
        token_id="x",
    )
    response = runtime.get("/api/v1/runtime/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
