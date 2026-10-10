"""The `/api/v1/ai` routes end to end: Core-issued tokens, the catalog in PostgreSQL, the
mock provider, quotas in an in-memory Redis, and usage rows in the ledger.

These tests commit real rows (`transaction=True`) because Runtime reads and writes them over
its own database connection, outside Django's test transaction.
"""

import json
from collections.abc import Iterator
from typing import Any

import fakeredis
import httpx
import pytest
from django.db import connection
from django.utils import timezone
from fastapi.testclient import TestClient

from nexa_core.ai.models import (
    AIModel,
    AIModelAlias,
    AIOrganizationAliasGrant,
    AIOrganizationPolicy,
    AIUsageEvent,
)
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import OrganizationMembership, Role
from nexa_core.organizations.services import create_organization
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app

pytestmark = pytest.mark.django_db(transaction=True)


def _test_database_url() -> str:
    d = connection.settings_dict
    return (
        f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
        f"{d['PORT'] or 5432}/{d['NAME']}"
    )


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings(
        nexa_env="test",
        runtime_database_url=_test_database_url(),
        redis_queue_url="redis://127.0.0.1:1/0",
        ai_registry_ttl_seconds=0.0,  # every request sees catalog changes at once
        ai_org_tokens_per_day=50_000,
        ai_user_requests_per_minute=5,
    )


@pytest.fixture
def runtime(settings: RuntimeSettings) -> Iterator[TestClient]:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    app = create_app(settings, jwks_client=JWKSClient(settings.core_jwks_url, transport=transport))
    with TestClient(app, raise_server_exceptions=False) as client:
        # Quotas against an in-memory Redis instead of the unreachable one in settings.
        quota = RedisQuota(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()), settings)
        app.state.quota = quota
        app.state.gateway.quota = quota
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


def _say(text: str, **extra: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}], "feature": "playground", **extra}


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            events.append((name, data))
    return events


class TestAuthentication:
    def test_token_required(self, runtime: TestClient) -> None:
        assert runtime.get("/api/v1/ai/models/").status_code == 401
        assert runtime.post("/api/v1/ai/chat/", json=_say("hi")).status_code == 401

    def test_permission_required(self, runtime: TestClient, owner: User) -> None:
        membership = OrganizationMembership.objects.get(user=owner)
        role = Role.objects.create(organization=membership.organization, key="no-ai", name="No AI")
        membership.role = role
        membership.save()
        response = runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=_bearer(owner))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
        assert AIUsageEvent.objects.count() == 0

    def test_identity_cannot_come_from_the_body(self, runtime: TestClient, owner: User) -> None:
        body = _say("hi") | {"organization_id": "11111111-1111-1111-1111-111111111111"}
        response = runtime.post("/api/v1/ai/chat/", json=body, headers=_bearer(owner))
        assert response.status_code == 422


class TestModels:
    def test_lists_models_the_organization_may_use(self, runtime: TestClient, owner: User) -> None:
        response = runtime.get("/api/v1/ai/models/", headers=_bearer(owner))
        assert response.status_code == 200, response.text
        models = {m["name"]: m for m in response.json()["data"]}
        assert set(models) == {"nexa-balanced", "nexa-fast"}  # reasoning needs a grant
        assert models["nexa-balanced"]["is_default"] is True
        assert "streaming" in models["nexa-fast"]["capabilities"]
        assert models["nexa-fast"]["max_output_tokens"] > 0

    def test_grants_are_per_organization(self, runtime: TestClient, owner: User) -> None:
        other = User.objects.create_user("other@example.com", "correct-horse-battery-staple")
        other.email_verified_at = timezone.now()
        other.save(update_fields=["email_verified_at"])
        other_org = create_organization(name="Other", owner=other)
        AIOrganizationAliasGrant.objects.create(
            organization=other_org, alias=AIModelAlias.objects.get(name="nexa-reasoning")
        )
        mine = runtime.get("/api/v1/ai/models/", headers=_bearer(owner)).json()["data"]
        theirs = runtime.get("/api/v1/ai/models/", headers=_bearer(other)).json()["data"]
        assert "nexa-reasoning" not in {m["name"] for m in mine}
        assert "nexa-reasoning" in {m["name"] for m in theirs}
        refused = runtime.post(
            "/api/v1/ai/chat/", json=_say("hi", model="nexa-reasoning"), headers=_bearer(owner)
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "AI_MODEL_UNAVAILABLE"

    def test_kill_switch(self, runtime: TestClient, owner: User) -> None:
        AIModel.objects.filter(provider="mock").update(is_active=False)
        response = runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=_bearer(owner))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_PROVIDER_UNAVAILABLE"
        assert runtime.get("/api/v1/ai/models/", headers=_bearer(owner)).json()["data"] == []


class TestChat:
    def test_success_and_usage_row(self, runtime: TestClient, owner: User) -> None:
        response = runtime.post(
            "/api/v1/ai/chat/", json=_say("Explain Django middleware"), headers=_bearer(owner)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["model"] == "nexa-balanced"
        assert data["provider"] == "mock"  # the only provider configured in tests
        assert "Explain Django middleware" in data["content"]
        assert data["usage"]["total_tokens"] == (
            data["usage"]["input_tokens"] + data["usage"]["output_tokens"]
        )
        assert data["usage"]["cost_usd"] == "0.000000"  # the mock model is priced at zero

        event = AIUsageEvent.objects.get(id=data["id"])
        membership = OrganizationMembership.objects.get(user=owner)
        assert event.organization_id == membership.organization_id
        assert event.user_id == owner.id
        assert event.feature == "playground"
        assert event.status == "success"
        assert event.request_id == response.headers["X-Request-ID"]
        assert event.total_tokens == data["usage"]["total_tokens"]
        assert event.price_id is not None

    def test_normalized_provider_errors(self, runtime: TestClient, owner: User) -> None:
        response = runtime.post(
            "/api/v1/ai/chat/", json=_say("[[fail:content]]"), headers=_bearer(owner)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AI_CONTENT_REJECTED"
        assert "mock" not in response.json()["error"]["message"]
        assert AIUsageEvent.objects.get().error_code == "AI_CONTENT_REJECTED"

    def test_structured_output(self, runtime: TestClient, owner: User) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "skills": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "skills"],
        }
        response = runtime.post(
            "/api/v1/ai/chat/",
            json=_say("Describe Ajit", response_format={"type": "json_schema", "schema": schema}),
            headers=_bearer(owner),
        )
        assert response.status_code == 200, response.text
        assert set(response.json()["data"]["parsed"]) == {"name", "skills"}

    def test_validation_errors(self, runtime: TestClient, owner: User) -> None:
        response = runtime.post("/api/v1/ai/chat/", json={"messages": []}, headers=_bearer(owner))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_ai_disabled_for_the_organization(self, runtime: TestClient, owner: User) -> None:
        membership = OrganizationMembership.objects.get(user=owner)
        AIOrganizationPolicy.objects.create(organization=membership.organization, ai_enabled=False)
        response = runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=_bearer(owner))
        assert response.status_code == 403
        assert AIUsageEvent.objects.count() == 0

    def test_quota(self, runtime: TestClient, owner: User) -> None:
        headers = _bearer(owner)
        for _ in range(5):
            assert (
                runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=headers).status_code
                == 200
            )
        response = runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=headers)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "AI_QUOTA_EXCEEDED"
        assert "Retry-After" in response.headers
        assert AIUsageEvent.objects.count() == 5  # the refused request never ran


class TestStream:
    def test_events(self, runtime: TestClient, owner: User) -> None:
        with runtime.stream(
            "POST", "/api/v1/ai/chat/stream/", json=_say("Explain SSE"), headers=_bearer(owner)
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            body = response.read().decode()
        events = _events(body)
        names = [name for name, _ in events]
        assert names[0] == "message.start"
        assert names[-2:] == ["usage", "message.end"]
        assert events[0][1]["request_id"].startswith("req_")
        text = "".join(d["text"] for n, d in events if n == "message.delta")
        assert "Explain SSE" in text
        assert "id: 1\n" in body
        event = AIUsageEvent.objects.get()
        assert event.stream is True
        assert event.status == "success"

    def test_error_after_start_is_an_event(self, runtime: TestClient, owner: User) -> None:
        response = runtime.post(
            "/api/v1/ai/chat/stream/",
            json=_say("[[fail:stream_after:1]] hi"),
            headers=_bearer(owner),
        )
        assert response.status_code == 200
        events = _events(response.text)
        assert [n for n, _ in events] == ["message.start", "message.delta", "usage", "error"]
        assert events[-1][1]["code"] == "AI_PROVIDER_UNAVAILABLE"
        assert events[-2][1]["output_tokens"] > 0  # what the failed call used
        assert AIUsageEvent.objects.get().status == "error"

    def test_errors_before_the_stream_are_json(self, runtime: TestClient, owner: User) -> None:
        response = runtime.post(
            "/api/v1/ai/chat/stream/", json=_say("hi", model="nexa-nope"), headers=_bearer(owner)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "AI_MODEL_UNAVAILABLE"


class TestUsageEndpointOnCore:
    def test_core_serves_usage_for_the_active_organization(
        self, runtime: TestClient, owner: User, client: Any
    ) -> None:
        headers = _bearer(owner)
        runtime.post("/api/v1/ai/chat/", json=_say("hi"), headers=headers)
        response = client.get("/api/v1/ai/usage/?scope=me&days=7", headers=headers)
        assert response.status_code == 200, response.content
        data = response.json()["data"]
        assert data["totals"]["requests"] == 1
        assert data["by_model"][0]["alias"] == "nexa-balanced"
        assert len(data["recent"]) == 1
        org = client.get("/api/v1/ai/usage/?scope=organization", headers=headers)
        assert org.status_code == 200  # owners hold analytics.view
