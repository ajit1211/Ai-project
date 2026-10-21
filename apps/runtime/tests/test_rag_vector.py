"""Semantic and hybrid retrieval end to end (Phase 09), against PostgreSQL with pgvector and the
mock embedding provider: what the vector side finds, readiness and model gating, tenant and
document isolation, fusion, every fallback, the retrieval-options endpoint, usage, and
grounded answers in hybrid mode.

Skipped where pgvector is not installed. The mock provider's vectors hash words and character
trigrams: they show the pipeline works and tolerate typos, but are not semantic (see
docs/architecture/vector-search.md#evaluation).
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import fakeredis
import httpx
import pytest
from django.db import connection, transaction
from django.utils import timezone
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from nexa_ai.embeddings import MockEmbeddingProvider
from nexa_ai.providers.mock import MockProvider
from nexa_common.access import Principal
from nexa_core.ai.models import AIEmbeddingModel, AIUsageEvent
from nexa_core.celery import app as celery_app
from nexa_core.documents import embedding, indexing, vectors
from nexa_core.documents.models import Document, DocumentEmbedding
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import Organization, TeamMembership
from nexa_core.organizations.services import create_organization
from nexa_core.testing import add_member, indexed_document
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app
from nexa_runtime.rag import vector as vector_module
from nexa_runtime.rag.retrieval import Passage
from nexa_runtime.rag.service import Grounded, Scope

pytestmark = pytest.mark.django_db(transaction=True)

SECURITY = (
    "# Security\n\n## Credentials\n\nPasswords must contain at least fourteen characters. "
    "Never reuse a password.\n\n## Devices\n\nReport a lost laptop to the service desk "
    "within one hour."
)
TRAVEL = (
    "# Travel\n\n## Booking\n\nFlights are booked fourteen days before departure.\n\n"
    "## Allowance\n\nThe daily meal allowance abroad is fifty euros."
)


@pytest.fixture(autouse=True)
def pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    if not vectors.status(connection).ready:
        pytest.skip("needs PostgreSQL with the pgvector extension")
    # Core's embed task runs in this process, as the worker would.
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


def _url() -> str:
    d = connection.settings_dict
    return (
        f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
        f"{d['PORT'] or 5432}/{d['NAME']}"
    )


@contextmanager
def make_client(
    *, embeddings: MockEmbeddingProvider | None = None, **overrides: Any
) -> Iterator[TestClient]:
    values: dict[str, Any] = {
        "nexa_env": "test",
        "runtime_database_url": _url(),
        "redis_queue_url": "redis://127.0.0.1:1/0",
        "ai_registry_ttl_seconds": 0.0,
        "memory_enabled": False,
        "rag_semantic_status_ttl_seconds": 0.0,
        "rag_retrieval_mode": "hybrid",
    }
    values.update(overrides)
    settings = RuntimeSettings(**values)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    app = create_app(
        settings,
        jwks_client=JWKSClient(settings.core_jwks_url, transport=transport),
        providers={"mock": MockProvider()},
        embedding_providers={"mock": embeddings or MockEmbeddingProvider()},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        fake = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
        app.state.gateway.quota = RedisQuota(fake, settings)
        app.state.chat._cancel = CancelRegistry(fake)
        yield client


@pytest.fixture
def runtime() -> Iterator[TestClient]:
    with make_client() as client:
        yield client


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


def acme() -> Organization:
    return Organization.objects.get(name="Acme")


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {start_session(user, client_type='web').access_token}"}


def principal_for(user: User, organization: Organization) -> Principal:
    membership = user.memberships.get(organization=organization)
    return Principal(
        user_id=user.id,
        session_id=None,
        organization_id=organization.id,
        membership_id=membership.id,
        role_key=membership.role.key,
        permissions=frozenset(
            membership.role.role_permissions.values_list("permission__codename", flat=True)
        ),
        team_ids=frozenset(
            TeamMembership.objects.filter(membership=membership).values_list("team_id", flat=True)
        ),
    )


def ready_document(org: Organization, user: User, text: str, **extra: Any) -> Document:
    """Indexed and semantically ready (the embed task ran inline after indexing)."""
    document: Document = indexed_document(org, user, text, **extra)
    state = DocumentEmbedding.objects.get(document=document)
    if state.status != "ready":
        model = embedding.active_model()
        assert model is not None
        assert embedding.run_embedding(document.id, model.id, state.generation) == "ready"
    return document


def retrieve(
    client: TestClient, user: User, question: str, mode: str = "auto", **scope: Any
) -> Grounded:
    rag = client.app.state.rag  # type: ignore[attr-defined]
    principal = principal_for(user, acme())

    async def run() -> Grounded:
        grounded: Grounded = await rag.retrieve(
            principal,
            Scope(scope.pop("kind", "library"), retrieval_mode=mode, max_sources=6, **scope),
            question=question,
        )
        return grounded

    return client.portal.call(run)  # type: ignore[union-attr]


def texts(grounded: Grounded) -> list[str]:
    return [p.content for p in grounded.evidence.passages]


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


class TestSemantic:
    def test_vectors_find_what_keywords_miss(self, runtime: TestClient, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        question = "pasword lenght rulez"  # every word misspelt: no keyword matches
        assert texts(retrieve(runtime, owner, question, "lexical")) == []
        found = retrieve(runtime, owner, question, "semantic")
        assert found.retrieval_mode == "semantic"
        assert found.fallback is None
        assert "fourteen characters" in texts(found)[0]
        assert found.strategy == "pgvector-cosine/v1"

    def test_results_are_ordered_by_similarity(self, runtime: TestClient, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        ready_document(acme(), owner, TRAVEL, filename="travel.md")
        found = retrieve(runtime, owner, "daily meal allowance abroad", "semantic")
        similarities = [p.similarity for p in found.evidence.passages]
        assert similarities == sorted(similarities, reverse=True)  # type: ignore[type-var]
        assert "fifty euros" in texts(found)[0]
        assert all(s is not None and s >= 0.2 for s in similarities)  # the mock's floor

    def test_nothing_relevant_means_no_evidence(self, runtime: TestClient, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        found = retrieve(runtime, owner, "Who won the chess championship in Oslo?", "semantic")
        assert texts(found) == []

    def test_only_ready_documents_of_the_active_model_are_searched(
        self, runtime: TestClient, owner: User
    ) -> None:
        searched = ready_document(acme(), owner, SECURITY, filename="security.md")
        pending = indexed_document(
            acme(), owner, SECURITY.replace("fourteen", "twenty"), filename="pending.md"
        )
        DocumentEmbedding.objects.filter(document=pending).update(status="pending")
        stale = ready_document(
            acme(), owner, SECURITY.replace("fourteen", "sixteen"), filename="stale.md"
        )
        DocumentEmbedding.objects.filter(document=stale).update(source_generation=0)
        found = retrieve(runtime, owner, "password characters", "semantic")
        assert {p.document_id for p in found.evidence.passages} == {searched.id}

    def test_vectors_of_another_model_are_never_compared(
        self, runtime: TestClient, owner: User, settings: Any
    ) -> None:
        document = ready_document(acme(), owner, SECURITY, filename="security.md")
        other = AIEmbeddingModel.objects.create(
            key="nexa-embed-mock-wide",
            display_name="Wide",
            provider="mock",
            provider_model="mock-embed-hash",
            dimensions=128,
            version="hash-v2",
            relevance_floor=0.2,
        )
        with make_client(embedding_model=other.key) as client:
            # The document has vectors only for the default model: nothing to compare with.
            assert texts(retrieve(client, owner, "password characters", "semantic")) == []
            settings.NEXA_EMBEDDING_MODEL = other.key
            with transaction.atomic():
                state = embedding.request_embedding(Document.objects.get(id=document.id))
            assert state is not None
            embedding.run_embedding(document.id, other.id, state.generation)
            assert texts(retrieve(client, owner, "password characters", "semantic"))


class TestIsolation:
    def test_vector_and_hybrid_search_stay_inside_what_the_member_may_read(
        self, runtime: TestClient, owner: User
    ) -> None:
        member = _verified("member@example.com")
        add_member(acme(), member, "member")
        stranger = _verified("stranger@example.com")
        other = create_organization(name="Other", owner=stranger)
        shared = ready_document(
            acme(), owner, SECURITY, filename="shared.md", visibility="organization"
        )
        hidden = ready_document(
            acme(), owner, SECURITY.replace("fourteen", "nineteen"), filename="hidden.md"
        )
        foreign = ready_document(
            other,
            stranger,
            SECURITY.replace("fourteen", "six"),
            filename="foreign.md",
            visibility="organization",
        )
        gone = ready_document(
            acme(), member, SECURITY.replace("fourteen", "ten"), filename="gone.md"
        )
        Document.objects.filter(id=gone.id).update(deleted_at=timezone.now())
        for mode in ("semantic", "hybrid"):
            found = retrieve(runtime, member, "password characters length", mode)
            ids = {p.document_id for p in found.evidence.passages}
            assert ids == {shared.id}, mode
            assert not ids & {hidden.id, foreign.id, gone.id}

    def test_forged_document_ids_are_refused_before_any_search(
        self, runtime: TestClient, owner: User
    ) -> None:
        stranger = _verified("stranger@example.com")
        other = create_organization(name="Other", owner=stranger)
        foreign = ready_document(
            other, stranger, SECURITY, filename="foreign.md", visibility="organization"
        )
        headers = auth(owner)
        conversation = runtime.post("/api/v1/conversations/", json={}, headers=headers).json()
        for document_id in (foreign.id, uuid4()):
            response = runtime.post(
                f"/api/v1/conversations/{conversation['data']['id']}/messages/",
                json={
                    "content": "password length?",
                    "grounding": {"document_ids": [str(document_id)], "retrieval_mode": "hybrid"},
                },
                headers={**headers, "Accept": "text/event-stream"},
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "DOCUMENT_UNAVAILABLE"
        assert not AIUsageEvent.objects.filter(source_type="rag_query").exists()


class TestHybrid:
    def test_fusion_combines_both_lists_without_repeats(
        self, runtime: TestClient, owner: User
    ) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        ready_document(acme(), owner, TRAVEL, filename="travel.md")
        found = retrieve(runtime, owner, "How many characters must passwords contain?")
        assert found.retrieval_mode == "hybrid"
        assert found.strategy.startswith("rrf(k=60):")
        passages = list(found.evidence.passages)
        assert len({p.chunk_id for p in passages}) == len(passages)
        top = passages[0]
        assert "fourteen characters" in top.content
        assert (top.lexical_rank, top.vector_rank) == (1, 1)  # found by both, ranked first
        assert [p.score for p in passages] == sorted((p.score for p in passages), reverse=True)

    def test_the_same_question_gives_the_same_order(self, runtime: TestClient, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        ready_document(acme(), owner, TRAVEL, filename="travel.md")
        first = [
            p.chunk_id
            for p in retrieve(runtime, owner, "fourteen days or characters").evidence.passages
        ]
        second = [
            p.chunk_id
            for p in retrieve(runtime, owner, "fourteen days or characters").evidence.passages
        ]
        assert first == second

    def test_a_failed_question_embedding_falls_back_to_keywords(self, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        failing = MockEmbeddingProvider(failures=["provider"] * 10)
        with make_client(embeddings=failing) as client:
            found = retrieve(client, owner, "password characters")
            assert (found.retrieval_mode, found.fallback) == ("lexical", "query_embedding_failed")
            assert texts(found)  # keyword results still answer
            semantic_only = retrieve(client, owner, "password characters", "semantic")
            assert semantic_only.retrieval_mode == "lexical"
            assert semantic_only.fallback == "query_embedding_failed"
        errors = AIUsageEvent.objects.filter(feature="embedding", source_type="rag_query")
        assert errors.filter(status="error").count() >= 2

    def test_a_failed_vector_query_keeps_the_keyword_results(
        self, runtime: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")

        async def broken(self: Any, conn: Any, query: Any) -> list[Passage]:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1/0"))  # a real database error, then a raise
            raise SQLAlchemyError("unreachable")

        monkeypatch.setattr(vector_module.VectorRetriever, "search", broken)
        found = retrieve(runtime, owner, "password characters")
        assert (found.retrieval_mode, found.fallback) == ("lexical", "vector_search_failed")
        assert "fourteen characters" in texts(found)[0]

    def test_without_semantic_search_auto_is_quietly_lexical(self, owner: User) -> None:
        ready_document(acme(), owner, SECURITY, filename="security.md")
        with make_client(embedding_enabled=False) as client:
            auto = retrieve(client, owner, "password characters")
            assert (auto.retrieval_mode, auto.fallback) == ("lexical", None)
            asked = retrieve(client, owner, "password characters", "hybrid")
            assert (asked.retrieval_mode, asked.fallback) == ("lexical", "semantic_disabled")


class TestApi:
    def test_retrieval_options(self, runtime: TestClient, owner: User) -> None:
        response = runtime.get("/api/v1/ai/retrieval/", headers=auth(owner))
        assert response.status_code == 200
        data = response.json()["data"]
        assert data == {
            "modes": ["hybrid", "semantic", "lexical"],
            "default_mode": "hybrid",
            "semantic_available": True,
            "semantic_unavailable_reason": None,
            "embedding_model": "nexa-embed-mock",
        }
        with make_client(embedding_enabled=False) as client:
            off = client.get("/api/v1/ai/retrieval/", headers=auth(owner)).json()["data"]
            assert (off["modes"], off["default_mode"], off["semantic_unavailable_reason"]) == (
                ["lexical"],
                "lexical",
                "disabled",
            )
        assert runtime.get("/api/v1/ai/retrieval/").status_code == 401

    def test_a_grounded_answer_in_hybrid_mode(self, runtime: TestClient, owner: User) -> None:
        document = ready_document(acme(), owner, SECURITY, filename="security.md")
        headers = auth(owner)
        conversation = runtime.post("/api/v1/conversations/", json={}, headers=headers).json()
        response = runtime.post(
            f"/api/v1/conversations/{conversation['data']['id']}/messages/",
            json={
                "content": "How many characters must passwords contain?",
                "grounding": {"document_ids": [str(document.id)]},
            },
            headers={**headers, "Accept": "text/event-stream"},
        )
        stream = events(response.text)
        assert stream[-1][0] == "message_complete"
        message = stream[-1][1]["message"]
        grounding = message["grounding"]
        assert (grounding["retrieval_requested"], grounding["retrieval_mode"]) == ("auto", "hybrid")
        assert grounding["fallback"] is None
        assert grounding["outcome"] == "answered"
        cited = [s for s in message["sources"] if s["cited"]]
        assert cited
        assert cited[0]["document_id"] == str(document.id)
        assert "embedding" not in json.dumps(message["sources"])  # no vectors or internals
        usage = AIUsageEvent.objects.get(feature="embedding", source_type="rag_query")
        assert (usage.status, usage.alias, usage.user_id) == (
            "success",
            "nexa-embed-mock",
            owner.id,
        )

    def test_the_requested_mode_is_kept_for_regeneration(
        self, runtime: TestClient, owner: User
    ) -> None:
        document = ready_document(acme(), owner, SECURITY, filename="security.md")
        headers = {**auth(owner), "Accept": "text/event-stream"}
        conversation = runtime.post("/api/v1/conversations/", json={}, headers=auth(owner)).json()
        conversation_id = conversation["data"]["id"]
        first = runtime.post(
            f"/api/v1/conversations/{conversation_id}/messages/",
            json={
                "content": "password characters",
                "grounding": {"document_ids": [str(document.id)], "retrieval_mode": "semantic"},
            },
            headers=headers,
        )
        assert events(first.text)[-1][1]["message"]["grounding"]["retrieval_mode"] == "semantic"
        again = runtime.post(
            f"/api/v1/conversations/{conversation_id}/regenerate/", headers=headers
        )
        grounding = events(again.text)[-1][1]["message"]["grounding"]
        assert (grounding["retrieval_requested"], grounding["retrieval_mode"]) == (
            "semantic",
            "semantic",
        )


def test_deleting_a_document_removes_it_from_semantic_search(
    runtime: TestClient, owner: User
) -> None:
    document = ready_document(acme(), owner, SECURITY, filename="security.md")
    assert texts(retrieve(runtime, owner, "password characters", "semantic"))
    with transaction.atomic():
        Document.objects.filter(id=document.id).update(deleted_at=timezone.now())
        indexing.forget_index(document)
    assert texts(retrieve(runtime, owner, "password characters", "semantic")) == []
