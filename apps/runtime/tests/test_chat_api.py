"""The chat engine end to end: Core-issued tokens, the conversation tables in PostgreSQL, the
AI Gateway with the mock provider, and the `nexa.chat.v1` stream.

These tests commit real rows (`transaction=True`) because Runtime reads and writes them over
its own connection, outside Django's test transaction. Starlette's test client buffers a
streamed response, so cases that need to act *during* a stream (stopping it, disconnecting,
a second send while the first runs) drive the service directly inside the app's event loop.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import timedelta
from typing import Any
from uuid import UUID

import fakeredis
import httpx
import pytest
from django.db import connection
from django.utils import timezone
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexa_ai.providers.base import ProviderCall, ProviderEvent, StreamEnd, TextChunk
from nexa_ai.providers.mock import MockProvider
from nexa_ai.types import FinishReason
from nexa_common.access import Principal
from nexa_core.ai.models import AIUsageEvent
from nexa_core.chat.models import Conversation, Message
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import Organization, RolePermission
from nexa_core.organizations.services import create_organization
from nexa_core.testing import add_member
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.chat.events import MessageStart, TextDelta, UsageReport
from nexa_runtime.chat.schemas import Exchange, SendMessage
from nexa_runtime.chat.service import ChatService, Generation, GenerationInProgress
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app

pytestmark = pytest.mark.django_db(transaction=True)

STREAM = {"Accept": "text/event-stream"}


def _test_database_url() -> str:
    d = connection.settings_dict
    return (
        f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
        f"{d['PORT'] or 5432}/{d['NAME']}"
    )


def make_settings(**overrides: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "nexa_env": "test",
        "runtime_database_url": _test_database_url(),
        "redis_queue_url": "redis://127.0.0.1:1/0",
        "ai_registry_ttl_seconds": 0.0,
    }
    values.update(overrides)
    return RuntimeSettings(**values)


@contextmanager
def make_client(provider: MockProvider | None = None, **overrides: Any) -> Iterator[TestClient]:
    settings = make_settings(**overrides)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    app = create_app(
        settings,
        jwks_client=JWKSClient(settings.core_jwks_url, transport=transport),
        providers={"mock": provider or MockProvider()},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        fake = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
        quota = RedisQuota(fake, settings)
        app.state.gateway.quota = quota
        app.state.chat._cancel = CancelRegistry(fake)
        yield client


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def runtime(provider: MockProvider) -> Iterator[TestClient]:
    with make_client(provider) as client:
        yield client


Runtime = Callable[..., TestClient]


@pytest.fixture
def make_runtime() -> Iterator[Runtime]:
    """Clients with custom settings or providers, all shut down after the test."""
    with ExitStack() as stack:

        def factory(provider: MockProvider | None = None, **overrides: Any) -> TestClient:
            return stack.enter_context(make_client(provider, **overrides))

        yield factory


def _verified(email: str) -> User:
    user = User.objects.create_user(email, "correct-horse-battery-staple")
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


@pytest.fixture
def owner() -> User:
    user = _verified("owner@example.com")
    create_organization(name="Acme", owner=user)
    return user


@pytest.fixture
def colleague(owner: User) -> User:
    """A member of the owner's organization."""
    user = _verified("colleague@example.com")
    add_member(Organization.objects.get(name="Acme"), user, "member")
    return user


@pytest.fixture
def outsider() -> User:
    user = _verified("outsider@example.com")
    create_organization(name="Other", owner=user)
    return user


def auth(user: User) -> dict[str, str]:
    issued = start_session(user, client_type="web")
    return {"Authorization": f"Bearer {issued.access_token}"}


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            found.append((name, data))
    return found


def new_conversation(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = client.post("/api/v1/conversations/", json=body, headers=headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def say(
    client: TestClient, headers: dict[str, str], conversation_id: str, text: str, **extra: Any
) -> list[tuple[str, dict[str, Any]]]:
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/",
        json={"content": text, **extra},
        headers={**headers, **STREAM},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return events(response.text)


# --- Conversations ---------------------------------------------------------------------------


class TestConversations:
    def test_create_list_get_rename_delete(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        created = new_conversation(runtime, headers, model="nexa-fast")
        assert created["title"] == ""
        assert created["model"] == "nexa-fast"
        assert created["generating"] is False

        listed = runtime.get("/api/v1/conversations/", headers=headers).json()
        assert [c["id"] for c in listed["data"]] == [created["id"]]
        assert listed["meta"] == {"next_cursor": None, "has_more": False}

        one = runtime.get(f"/api/v1/conversations/{created['id']}/", headers=headers)
        assert one.json()["data"]["id"] == created["id"]

        renamed = runtime.patch(
            f"/api/v1/conversations/{created['id']}/", json={"title": "Middleware"}, headers=headers
        )
        assert renamed.json()["data"]["title"] == "Middleware"

        deleted = runtime.delete(f"/api/v1/conversations/{created['id']}/", headers=headers)
        assert deleted.status_code == 200
        assert (
            runtime.get(f"/api/v1/conversations/{created['id']}/", headers=headers).status_code
            == 404
        )

    def test_first_message_titles_the_conversation_and_empty_title_restores_it(
        self, runtime: TestClient, owner: User
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        say(runtime, headers, conversation["id"], "Explain Django middleware\nin detail")
        url = f"/api/v1/conversations/{conversation['id']}/"
        assert (
            runtime.get(url, headers=headers).json()["data"]["title"] == "Explain Django middleware"
        )
        runtime.patch(url, json={"title": "Custom"}, headers=headers)
        restored = runtime.patch(url, json={"title": ""}, headers=headers).json()["data"]
        assert restored["title"] == "Explain Django middleware"

    def test_archive_hides_and_blocks_sending(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        url = f"/api/v1/conversations/{conversation['id']}/"
        runtime.patch(url, json={"status": "archived"}, headers=headers)
        assert runtime.get("/api/v1/conversations/", headers=headers).json()["data"] == []
        archived = runtime.get("/api/v1/conversations/?status=archived", headers=headers).json()
        assert [c["id"] for c in archived["data"]] == [conversation["id"]]
        refused = runtime.post(
            f"{url}messages/", json={"content": "hi"}, headers={**headers, **STREAM}
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "CONFLICT"

    def test_search_and_cursor_pagination(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        ids = [new_conversation(runtime, headers, title=f"Topic {n}")["id"] for n in range(5)]
        page = runtime.get("/api/v1/conversations/?limit=2", headers=headers).json()
        assert [c["id"] for c in page["data"]] == ids[::-1][:2]  # newest first
        assert page["meta"]["has_more"] is True
        rest = runtime.get(
            f"/api/v1/conversations/?limit=10&cursor={page['meta']['next_cursor']}", headers=headers
        ).json()
        assert [c["id"] for c in rest["data"]] == ids[::-1][2:]
        found = runtime.get("/api/v1/conversations/?q=topic 3", headers=headers).json()["data"]
        assert [c["id"] for c in found] == [ids[3]]
        assert runtime.get("/api/v1/conversations/?q=100%", headers=headers).json()["data"] == []
        bad = runtime.get("/api/v1/conversations/?cursor=not-a-cursor", headers=headers)
        assert bad.status_code == 422

    def test_activity_orders_the_list(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        older = new_conversation(runtime, headers)
        new_conversation(runtime, headers)
        say(runtime, headers, older["id"], "bump")
        listed = runtime.get("/api/v1/conversations/", headers=headers).json()["data"]
        assert listed[0]["id"] == older["id"]

    def test_model_is_validated_server_side(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        for model in ("nexa-nope", "nexa-reasoning"):  # unknown; restricted without a grant
            response = runtime.post(
                "/api/v1/conversations/", json={"model": model}, headers=headers
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "AI_MODEL_UNAVAILABLE"

    def test_text_with_nul_is_refused(self, runtime: TestClient, owner: User) -> None:
        # PostgreSQL text cannot hold NUL: a 422 here, not a database error (whose message
        # would carry the text) and a 500.
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        base = f"/api/v1/conversations/{conversation['id']}/"
        attempts = [
            runtime.post("/api/v1/conversations/", json={"title": "a\x00b"}, headers=headers),
            runtime.patch(base, json={"title": "a\x00b"}, headers=headers),
            runtime.post(
                f"{base}messages/", json={"content": "a\x00b"}, headers={**headers, **STREAM}
            ),
            runtime.get("/api/v1/conversations/", params={"q": "a\x00b"}, headers=headers),
        ]
        assert [r.status_code for r in attempts] == [422] * len(attempts)
        assert {r.json()["error"]["code"] for r in attempts} == {"VALIDATION_FAILED"}
        assert not Message.objects.exists()

    def test_database_errors_do_not_carry_message_text(self, runtime: TestClient) -> None:
        engine = runtime.app.state.engine  # type: ignore[attr-defined]

        async def scenario() -> str:
            try:
                async with engine.connect() as conn:
                    await conn.execute(
                        text("SELECT 1 / (length(CAST(:words AS text)) * 0)"),
                        {"words": "private words"},
                    )
            except DBAPIError as exc:
                return str(exc)
            return ""

        message = runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert "division by zero" in message
        assert "private words" not in message

    def test_creation_is_rate_limited(self, make_runtime: Runtime, owner: User) -> None:
        client = make_runtime(chat_conversations_per_minute=2)
        headers = auth(owner)
        for _ in range(2):
            new_conversation(client, headers)
        refused = client.post("/api/v1/conversations/", json={}, headers=headers)
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in refused.headers


# --- Authorization ---------------------------------------------------------------------------


class TestAuthorization:
    def test_requires_a_token(self, runtime: TestClient) -> None:
        assert runtime.get("/api/v1/conversations/").status_code == 401

    @pytest.mark.parametrize("who", ["colleague", "outsider"])
    def test_others_cannot_touch_a_private_conversation(
        self, runtime: TestClient, owner: User, who: str, request: pytest.FixtureRequest
    ) -> None:
        conversation = new_conversation(runtime, auth(owner))
        say(runtime, auth(owner), conversation["id"], "private thoughts")
        intruder = auth(request.getfixturevalue(who))
        base = f"/api/v1/conversations/{conversation['id']}/"
        attempts = [
            runtime.get(base, headers=intruder),
            runtime.patch(base, json={"title": "mine now"}, headers=intruder),
            runtime.delete(base, headers=intruder),
            runtime.get(f"{base}messages/", headers=intruder),
            runtime.post(
                f"{base}messages/", json={"content": "hi"}, headers={**intruder, **STREAM}
            ),
            runtime.post(f"{base}regenerate/", headers={**intruder, **STREAM}),
            runtime.post(f"{base}cancel/", headers=intruder),
        ]
        assert [r.status_code for r in attempts] == [404] * len(attempts)
        assert runtime.get("/api/v1/conversations/", headers=intruder).json()["data"] == []
        assert Conversation.objects.get(id=conversation["id"]).title == "private thoughts"

    def test_client_cannot_set_ownership_role_or_organization(
        self, runtime: TestClient, owner: User, outsider: User
    ) -> None:
        headers = auth(owner)
        create = runtime.post(
            "/api/v1/conversations/", json={"owner_id": str(outsider.id)}, headers=headers
        )
        assert create.status_code == 422
        conversation = new_conversation(runtime, headers)
        base = f"/api/v1/conversations/{conversation['id']}/"
        patch = runtime.patch(base, json={"organization_id": str(outsider.id)}, headers=headers)
        assert patch.status_code == 422
        system = runtime.post(
            f"{base}messages/", json={"content": "hi", "role": "system"}, headers=headers
        )
        assert system.status_code == 422

    def test_permissions_are_enforced(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        RolePermission.objects.filter(permission__codename="ai.chat").delete()
        refused = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/messages/",
            json={"content": "hi"},
            headers={**headers, **STREAM},
        )
        assert refused.status_code == 403
        RolePermission.objects.filter(permission__codename="conversation.create").delete()
        assert runtime.post("/api/v1/conversations/", json={}, headers=headers).status_code == 403


# --- Generation ------------------------------------------------------------------------------


class TestGeneration:
    def test_stream_event_sequence_and_persistence(
        self, runtime: TestClient, owner: User, provider: MockProvider
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        response = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/messages/",
            json={"content": "Explain Django middleware"},
            headers={**headers, **STREAM},
        )
        assert response.headers["x-nexa-stream-protocol"] == "nexa.chat.v1"
        stream = events(response.text)
        names = [name for name, _ in stream]
        assert names[0] == "message_start"
        assert names[-2:] == ["usage", "message_complete"]
        assert names.count("text_delta") > 3
        assert set(names[1:-2]) == {"text_delta"}

        start = stream[0][1]
        assert start["protocol"] == "nexa.chat.v1"
        assert start["user_message"]["content"] == "Explain Django middleware"
        assert start["assistant_message"]["status"] == "pending"
        assistant_id = start["assistant_message"]["id"]
        assert all(d["message_id"] == assistant_id for n, d in stream if n == "text_delta")

        final = stream[-1][1]["message"]
        streamed = "".join(d["text"] for n, d in stream if n == "text_delta")
        assert final["status"] == "completed"
        assert final["content"] == streamed
        assert final["provider"] == "mock"
        assert final["output_tokens"] > 0
        assert final["latency_ms"] >= 0
        assert final["ttft_ms"] is not None

        stored = Message.objects.get(id=assistant_id)
        assert stored.status == "completed"
        assert stored.content == streamed
        assert stored.request_id == response.headers["X-Request-ID"]
        conversation_row = Conversation.objects.get(id=conversation["id"])
        assert conversation_row.message_count == 2
        assert conversation_row.active_generation_id is None

        # Usage: one ledger row, linked both ways, labelled as chat traffic.
        usage = AIUsageEvent.objects.get()
        assert stored.usage_event_id == usage.id
        assert usage.feature == "chat"
        assert usage.source_type == "message"
        assert usage.source_id == stored.id
        assert usage.output_tokens == stored.output_tokens

    def test_messages_are_paginated_oldest_first(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        for n in range(3):
            say(runtime, headers, conversation["id"], f"question {n}")
        base = f"/api/v1/conversations/{conversation['id']}/messages/"
        newest = runtime.get(f"{base}?limit=4", headers=headers).json()
        assert [m["sequence"] for m in newest["data"]] == [3, 4, 5, 6]
        assert newest["meta"] == {"next_before": 3, "has_more": True}
        older = runtime.get(f"{base}?limit=4&before=3", headers=headers).json()
        assert [m["sequence"] for m in older["data"]] == [1, 2]
        assert older["meta"]["has_more"] is False

    def test_context_is_bounded_and_from_this_conversation_only(
        self, make_runtime: Runtime, owner: User, provider: MockProvider
    ) -> None:
        client = make_runtime(provider, chat_context_max_messages=3)
        headers = auth(owner)
        other = new_conversation(client, headers)
        say(client, headers, other["id"], "SECRET from another conversation")
        conversation = new_conversation(client, headers)
        for n in range(3):
            say(client, headers, conversation["id"], f"turn {n}")
        sent = provider.calls[-1].request.messages
        assert sent[0].role.value == "system"
        assert len(sent) == 1 + 3
        assert sent[-1].text == "turn 2"
        assert all("SECRET" not in m.text for m in sent)

    def test_non_streaming_reply(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        response = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/messages/",
            json={"content": "hello"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["user_message"]["role"] == "user"
        assert data["assistant_message"]["status"] == "completed"
        assert data["assistant_message"]["content"]

    @pytest.mark.parametrize(
        ("phrase", "code"),
        [("[[fail:content]]", "AI_CONTENT_REJECTED"), ("[[fail:timeout]]", "AI_TIMEOUT")],
    )
    def test_provider_failure_keeps_the_question_and_fails_the_reply(
        self, runtime: TestClient, owner: User, phrase: str, code: str
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        stream = say(runtime, headers, conversation["id"], f"{phrase} hello")
        # What the failed call used comes first, then the error.
        assert [n for n, _ in stream] == ["message_start", "usage", "error"]
        error = stream[-1][1]
        assert error["code"] == code
        assert error["assistant_message"]["status"] == "failed"
        assert "mock" not in error["message"]
        user = Message.objects.get(role="user")
        assert user.content == f"{phrase} hello"
        reply = Message.objects.get(role="assistant")
        assert (reply.status, reply.error_code) == ("failed", code)
        assert reply.usage_event_id == AIUsageEvent.objects.get().id
        assert Conversation.objects.get().active_generation_id is None

    def test_failure_mid_stream_keeps_the_partial_text(
        self, runtime: TestClient, owner: User
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        stream = say(runtime, headers, conversation["id"], "[[fail:stream_after:2]] hello")
        names = [n for n, _ in stream]
        assert names == ["message_start", "text_delta", "text_delta", "usage", "error"]
        partial = "".join(d["text"] for n, d in stream if n == "text_delta")
        reply = Message.objects.get(role="assistant")
        assert reply.status == "failed"  # never "completed" with half an answer
        assert reply.content == partial
        assert reply.error_code == "AI_PROVIDER_UNAVAILABLE"
        # The tokens the failed call used are on the reply and linked to the ledger.
        usage = AIUsageEvent.objects.get()
        assert reply.output_tokens == usage.output_tokens > 0
        assert reply.usage_event_id == usage.id
        assert stream[-1][1]["assistant_message"]["output_tokens"] == reply.output_tokens

    def test_a_failed_non_streaming_reply_is_linked_to_its_usage(
        self, runtime: TestClient, owner: User
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        response = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/messages/",
            json={"content": "[[fail:timeout]] hello"},
            headers=headers,
        )
        assert response.json()["error"]["code"] == "AI_TIMEOUT"
        reply = Message.objects.get(role="assistant")
        assert (reply.status, reply.error_code) == ("failed", "AI_TIMEOUT")
        assert reply.usage_event_id == AIUsageEvent.objects.get().id

    def test_nul_from_the_model_is_dropped(self, make_runtime: Runtime, owner: User) -> None:
        class Nul(MockProvider):
            async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
                yield TextChunk("Hel\x00lo")
                yield TextChunk("\x00")
                yield StreamEnd(finish_reason=FinishReason.STOP)

        client = make_runtime(Nul())
        headers = auth(owner)
        conversation = new_conversation(client, headers)
        stream = say(client, headers, conversation["id"], "hi")
        assert stream[-1][0] == "message_complete"
        assert [d["text"] for n, d in stream if n == "text_delta"] == ["Hello"]
        assert Message.objects.get(role="assistant").content == "Hello"

    def test_an_unexpected_error_fails_the_reply_and_unlocks(
        self, runtime: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(gen: Generation) -> None:
            raise RuntimeError("a bug")

        monkeypatch.setattr(runtime.app.state.chat, "_prepare", broken)  # type: ignore[attr-defined]
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        url = f"/api/v1/conversations/{conversation['id']}/messages/"
        response = runtime.post(url, json={"content": "hello"}, headers=headers)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "AI_INTERNAL_ERROR"
        reply = Message.objects.get(role="assistant")
        assert (reply.status, reply.error_code) == ("failed", "AI_INTERNAL_ERROR")
        assert Conversation.objects.get().active_generation_id is None
        stream = say(runtime, headers, conversation["id"], "again")  # streaming: the same
        assert [n for n, _ in stream] == ["message_start", "error"]
        assert stream[-1][1]["code"] == "AI_INTERNAL_ERROR"
        assert Conversation.objects.get().active_generation_id is None

    def test_a_failed_final_write_still_ends_the_stream_and_unlocks(
        self, runtime: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(runtime.app.state.chat, "_finalize", broken)  # type: ignore[attr-defined]
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        stream = say(runtime, headers, conversation["id"], "hello")
        assert stream[-1][0] == "error"  # a terminal event, not a stream that just stops
        assert stream[-1][1]["code"] == "AI_INTERNAL_ERROR"
        assert stream[-1][1]["assistant_message"]["status"] == "failed"
        reply = Message.objects.get(role="assistant")
        assert (reply.status, reply.error_code) == ("failed", "AI_INTERNAL_ERROR")
        assert Conversation.objects.get().active_generation_id is None
        monkeypatch.undo()
        assert say(runtime, headers, conversation["id"], "again")[-1][0] == "message_complete"

    def test_finishing_a_reply_locks_the_conversation_first(
        self, runtime: TestClient, owner: User
    ) -> None:
        # Sending and regenerating lock the conversation and then write messages; finishing a
        # reply takes the same order, so two of them cannot deadlock.
        statements: list[str] = []
        engine = runtime.app.state.engine.sync_engine  # type: ignore[attr-defined]

        def record(*args: Any) -> None:
            statements.append(" ".join(args[2].split()[:2]))

        sa_event.listen(engine, "before_cursor_execute", record)
        try:
            headers = auth(owner)
            conversation = new_conversation(runtime, headers)
            stream = say(runtime, headers, conversation["id"], "hello")
        finally:
            sa_event.remove(engine, "before_cursor_execute", record)
        assert stream[-1][0] == "message_complete"
        writes = [s for s in statements if s in ("UPDATE conversations", "UPDATE messages")]
        assert writes[-3:] == ["UPDATE conversations", "UPDATE conversations", "UPDATE messages"]

    def test_empty_answer_is_a_retryable_failure(self, make_runtime: Runtime, owner: User) -> None:
        class Silent(MockProvider):
            async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
                yield StreamEnd(finish_reason=FinishReason.STOP)

        client = make_runtime(Silent())
        headers = auth(owner)
        conversation = new_conversation(client, headers)
        stream = say(client, headers, conversation["id"], "hello")
        assert stream[-1][0] == "error"
        assert stream[-1][1]["code"] == "AI_EMPTY_RESPONSE"
        assert stream[-1][1]["retryable"] is True

    def test_quota_refusal_after_the_message_is_stored(
        self, make_runtime: Runtime, owner: User
    ) -> None:
        client = make_runtime(ai_user_requests_per_minute=1)
        headers = auth(owner)
        conversation = new_conversation(client, headers)
        say(client, headers, conversation["id"], "first")
        stream = say(client, headers, conversation["id"], "second")
        assert [n for n, _ in stream] == ["message_start", "error"]
        assert stream[-1][1]["code"] == "AI_QUOTA_EXCEEDED"
        assert stream[-1][1]["retry_after"] >= 1
        assert Message.objects.filter(content="second").exists()  # the question is kept


class TestRetries:
    def test_client_message_id_makes_sending_idempotent(
        self, runtime: TestClient, owner: User
    ) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        first = say(runtime, headers, conversation["id"], "hello", client_message_id="abc-1")
        replay = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/messages/",
            json={"content": "hello", "client_message_id": "abc-1"},
            headers={**headers, **STREAM},
        )
        assert replay.status_code == 200
        assert replay.headers["content-type"].startswith("application/json")
        data = replay.json()["data"]
        assert data["user_message"]["id"] == first[0][1]["user_message"]["id"]
        assert data["assistant_message"]["id"] == first[0][1]["assistant_message"]["id"]
        assert Message.objects.count() == 2
        assert AIUsageEvent.objects.count() == 1  # no second model call

    def test_regenerate_replaces_the_last_reply(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        first = say(runtime, headers, conversation["id"], "hello")
        response = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/regenerate/", headers={**headers, **STREAM}
        )
        again = events(response.text)
        assert again[0][1]["user_message"]["id"] == first[0][1]["user_message"]["id"]
        assert again[-1][0] == "message_complete"
        assert Message.objects.count() == 2
        assert not Message.objects.filter(id=first[0][1]["assistant_message"]["id"]).exists()
        assert AIUsageEvent.objects.count() == 2  # both calls stay in the ledger

    def test_retry_after_a_failure(self, make_runtime: Runtime, owner: User) -> None:
        provider = MockProvider(failures={"mock-1": ["provider", "provider", "provider"]})
        client = make_runtime(provider)
        headers = auth(owner)
        conversation = new_conversation(client, headers)
        failed = say(client, headers, conversation["id"], "hello")
        assert failed[-1][0] == "error"
        response = client.post(
            f"/api/v1/conversations/{conversation['id']}/regenerate/", headers={**headers, **STREAM}
        )
        retried = events(response.text)
        assert retried[-1][0] == "message_complete"
        assert [m.status for m in Message.objects.order_by("sequence")] == [
            "completed",
            "completed",
        ]

    def test_nothing_to_regenerate(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation = new_conversation(runtime, headers)
        response = runtime.post(
            f"/api/v1/conversations/{conversation['id']}/regenerate/", headers=headers
        )
        assert response.status_code == 409


# --- During a generation (service level, inside the app's event loop) ------------------------


def _principal(user: User) -> Principal:
    membership = user.memberships.get()
    return Principal(
        user_id=user.id,
        session_id=None,
        organization_id=membership.organization_id,
        membership_id=membership.id,
        role_key=membership.role.key,
        permissions=frozenset(
            membership.role.role_permissions.values_list("permission__codename", flat=True)
        ),
    )


class TestDuringGeneration:
    def test_stop_ends_the_stream_as_cancelled(self, runtime: TestClient, owner: User) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def scenario() -> list[str]:
            gen = await service.send(
                principal,
                conversation_id,
                SendMessage(content="[[slow:300]] tell me a story"),
                request_id="req_t",
            )
            assert isinstance(gen, Generation)
            names: list[str] = []
            async for event in service.stream(gen):
                kind = getattr(event, "type", "ping")
                names.append(kind)
                if isinstance(event, TextDelta) and names.count("text_delta") == 1:
                    result = await service.cancel(principal, conversation_id)
                    assert result.cancelled
            await runtime.app.state.gateway.drain()  # type: ignore[attr-defined]
            return names

        names = runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert names[0] == "message_start"
        assert names[-1] == "message_cancelled"
        reply = Message.objects.get(role="assistant")
        assert reply.status == "cancelled"
        assert reply.content  # the text that had arrived is kept
        assert Conversation.objects.get().active_generation_id is None
        # What the stopped call used is on the reply and linked to the ledger.
        usage = AIUsageEvent.objects.get()
        assert usage.status == "cancelled"
        assert reply.usage_event_id == usage.id
        assert reply.output_tokens == usage.output_tokens > 0

    def test_a_stop_before_the_model_starts_releases_the_quota(
        self, make_runtime: Runtime, owner: User
    ) -> None:
        # One request at a time: a reservation that leaked would refuse the second reply.
        client = make_runtime(ai_user_concurrent_requests=1)
        conversation_id = UUID(new_conversation(client, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = client.app.state.chat  # type: ignore[attr-defined]
        gateway = client.app.state.gateway  # type: ignore[attr-defined]

        async def scenario() -> tuple[UUID, list[str]]:
            gen = await service.send(
                principal, conversation_id, SendMessage(content="[[slow:300]] hi"), request_id=None
            )
            assert isinstance(gen, Generation)
            names: list[str] = []
            async for event in service.stream(gen):
                names.append(getattr(event, "type", "ping"))
                if isinstance(event, MessageStart):  # before the gateway is even called
                    assert (await service.cancel(principal, conversation_id)).cancelled
            await gateway.drain()
            again = await service.send(
                principal, conversation_id, SendMessage(content="hello"), request_id=None
            )
            assert isinstance(again, Generation)
            async for event in service.stream(again):
                names.append(getattr(event, "type", "ping"))
            return gen.assistant_message.id, names

        stopped_id, names = client.portal.call(scenario)  # type: ignore[union-attr]
        assert names[:2] == ["message_start", "message_cancelled"]
        assert names[-1] == "message_complete"  # the reservation was released
        stopped = Message.objects.get(id=stopped_id)
        assert stopped.status == "cancelled"
        usage = AIUsageEvent.objects.get(source_id=stopped_id)
        assert usage.status == "cancelled"
        assert stopped.usage_event_id == usage.id

    def test_a_stream_closed_before_it_was_read_releases_the_conversation(
        self, runtime: TestClient, owner: User
    ) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def scenario() -> None:
            gen = await service.send(
                principal, conversation_id, SendMessage(content="hello"), request_id=None
            )
            assert isinstance(gen, Generation)
            # The client left before the response started: nothing is ever read.
            await service.stream(gen).aclose()
            await service.drain()

        runtime.portal.call(scenario)  # type: ignore[union-attr]
        reply = Message.objects.get(role="assistant")
        assert reply.status == "cancelled"
        assert Conversation.objects.get().active_generation_id is None
        assert not AIUsageEvent.objects.exists()  # the model was never called

    def test_a_stop_before_a_non_streaming_call_skips_the_model(
        self, runtime: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]
        mark_streaming = service._mark_streaming

        async def scenario() -> Exchange:
            async def stopped_meanwhile(message_id: UUID) -> None:
                await mark_streaming(message_id)
                assert (await service.cancel(principal, conversation_id)).cancelled

            monkeypatch.setattr(service, "_mark_streaming", stopped_meanwhile)
            gen = await service.send(
                principal, conversation_id, SendMessage(content="hello"), request_id=None
            )
            assert isinstance(gen, Generation)
            return await service.complete(gen)

        exchange = runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert exchange.assistant_message.status == "cancelled"
        assert not AIUsageEvent.objects.exists()  # no call, so no reservation to leak
        assert Conversation.objects.get().active_generation_id is None

    def test_leaving_during_the_final_write_still_finishes_it(
        self, runtime: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]
        finalize = service._finalize

        async def scenario() -> None:
            writing = asyncio.Event()

            async def slow_finalize(*args: Any, **kwargs: Any) -> Any:
                writing.set()
                await asyncio.sleep(0.3)
                return await finalize(*args, **kwargs)

            monkeypatch.setattr(service, "_finalize", slow_finalize)
            gen = await service.send(
                principal, conversation_id, SendMessage(content="hello"), request_id=None
            )
            assert isinstance(gen, Generation)

            async def consume() -> None:
                async for _ in service.stream(gen):
                    pass

            task = asyncio.create_task(consume())
            await writing.wait()
            task.cancel()  # the client leaves while the reply is being stored
            with suppress(asyncio.CancelledError):
                await task
            await service.drain()

        runtime.portal.call(scenario)  # type: ignore[union-attr]
        reply = Message.objects.get(role="assistant")
        assert reply.status == "completed"
        assert reply.content
        assert Conversation.objects.get().active_generation_id is None

    def test_stop_ends_a_non_streaming_reply(self, runtime: TestClient, owner: User) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]
        gateway = runtime.app.state.gateway  # type: ignore[attr-defined]

        async def scenario() -> Exchange:
            gen = await service.send(
                principal,
                conversation_id,
                SendMessage(content="[[slow:3000]] hello"),
                request_id=None,
            )
            assert isinstance(gen, Generation)
            task = asyncio.create_task(service.complete(gen))
            await asyncio.sleep(0.3)
            assert (await service.cancel(principal, conversation_id)).cancelled
            exchange = await asyncio.wait_for(task, 2)  # long before the model would answer
            await gateway.drain()
            return exchange

        exchange = runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert exchange.assistant_message.status == "cancelled"
        assert Message.objects.get(role="assistant").status == "cancelled"
        assert AIUsageEvent.objects.get().status == "cancelled"
        assert Conversation.objects.get().active_generation_id is None

    def test_a_stop_after_the_usage_report_changes_nothing(
        self, runtime: TestClient, owner: User
    ) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def scenario() -> list[str]:
            gen = await service.send(
                principal, conversation_id, SendMessage(content="hello"), request_id=None
            )
            assert isinstance(gen, Generation)
            names: list[str] = []
            async for event in service.stream(gen):
                names.append(getattr(event, "type", "ping"))
                if isinstance(event, UsageReport):  # the model has finished
                    assert (await service.cancel(principal, conversation_id)).cancelled
            return names

        names = runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert names[-2:] == ["usage", "message_complete"]
        assert Message.objects.get(role="assistant").status == "completed"

    def test_client_disconnect_saves_what_arrived(self, runtime: TestClient, owner: User) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def scenario() -> str:
            gen = await service.send(
                principal,
                conversation_id,
                SendMessage(content="[[slow:200]] a long answer"),
                request_id=None,
            )
            assert isinstance(gen, Generation)
            stream = service.stream(gen)
            received = ""
            async for event in stream:
                if isinstance(event, TextDelta):
                    received += event.text
                    break
            await stream.aclose()
            await service.drain()
            await runtime.app.state.gateway.drain()  # type: ignore[attr-defined]
            return received

        received = runtime.portal.call(scenario)  # type: ignore[union-attr]
        reply = Message.objects.get(role="assistant")
        assert reply.status == "cancelled"
        assert reply.content.startswith(received)
        assert Conversation.objects.get().active_generation_id is None
        usage = AIUsageEvent.objects.get()
        assert usage.status == "cancelled"
        assert reply.usage_event_id == usage.id

    def test_one_generation_at_a_time(self, runtime: TestClient, owner: User) -> None:
        conversation_id = UUID(new_conversation(runtime, auth(owner))["id"])
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def scenario() -> None:
            first = await service.send(
                principal, conversation_id, SendMessage(content="one"), request_id=None
            )
            assert isinstance(first, Generation)
            with pytest.raises(GenerationInProgress):
                await service.send(
                    principal, conversation_id, SendMessage(content="two"), request_id=None
                )
            with pytest.raises(GenerationInProgress):
                await service.delete(principal, conversation_id)
            async for event in service.stream(first):
                assert (
                    not isinstance(event, MessageStart)
                    or event.assistant_message.status == "pending"
                )
            again = await service.send(
                principal, conversation_id, SendMessage(content="three"), request_id=None
            )
            assert isinstance(again, Generation)
            async for _ in service.stream(again):
                pass

        runtime.portal.call(scenario)  # type: ignore[union-attr]
        assert [m.content for m in Message.objects.filter(role="user").order_by("sequence")] == [
            "one",
            "three",
        ]

    def _abandoned_reply(
        self, runtime: TestClient, owner: User, conversation_id: UUID, **message: Any
    ) -> UUID:
        """A reply claimed by a process that then died: never generated, never finished."""
        principal = _principal(owner)
        service: ChatService = runtime.app.state.chat  # type: ignore[attr-defined]

        async def claim() -> UUID:
            gen = await service.send(
                principal, conversation_id, SendMessage(**message), request_id=None
            )
            assert isinstance(gen, Generation)
            return UUID(str(gen.assistant_message.id))

        return runtime.portal.call(claim)  # type: ignore[union-attr]

    def test_a_stale_lock_from_a_dead_process_is_taken_over(
        self, runtime: TestClient, owner: User
    ) -> None:
        headers = auth(owner)
        conversation_id = UUID(new_conversation(runtime, headers)["id"])
        orphan_id = self._abandoned_reply(runtime, owner, conversation_id, content="hello")
        busy = runtime.post(
            f"/api/v1/conversations/{conversation_id}/messages/",
            json={"content": "too soon"},
            headers={**headers, **STREAM},
        )
        assert busy.status_code == 409
        Conversation.objects.filter(id=conversation_id).update(
            generation_started_at=timezone.now() - timedelta(hours=1)
        )
        stream = say(runtime, headers, str(conversation_id), "hello again")
        assert stream[-1][0] == "message_complete"
        # The reply the dead process left behind says so, instead of staying pending forever.
        orphan = Message.objects.get(id=orphan_id)
        assert (orphan.status, orphan.error_code) == ("failed", "GENERATION_INTERRUPTED")

    def test_replaying_a_message_whose_reply_died(self, runtime: TestClient, owner: User) -> None:
        headers = auth(owner)
        conversation_id = UUID(new_conversation(runtime, headers)["id"])
        self._abandoned_reply(
            runtime, owner, conversation_id, content="hello", client_message_id="abc-1"
        )
        url = f"/api/v1/conversations/{conversation_id}/messages/"
        retry = {"content": "hello", "client_message_id": "abc-1"}
        early = runtime.post(url, json=retry, headers={**headers, **STREAM})
        assert early.status_code == 409  # still being generated, as far as anyone knows
        assert early.json()["error"]["code"] == "GENERATION_IN_PROGRESS"
        Conversation.objects.filter(id=conversation_id).update(
            generation_started_at=timezone.now() - timedelta(hours=1)
        )
        replay = runtime.post(url, json=retry, headers={**headers, **STREAM})
        assert replay.status_code == 200
        reply = replay.json()["data"]["assistant_message"]
        assert (reply["status"], reply["error_code"]) == ("failed", "GENERATION_INTERRUPTED")
