"""The semantic index on the Core side (Phase 09): embedding runs, reuse of unchanged vectors,
re-embedding after changes, superseded and duplicate runs, partial failures, deletion, model
isolation, the daily budget, the API and the embedding-model registry.

Tests that store vectors need PostgreSQL with pgvector (CI, Docker, or any server where the
extension is installed); they are skipped elsewhere. Embeddings come from the deterministic
mock provider (development and test only).
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from nexa_ai.embeddings import MockEmbeddingProvider
from nexa_ai.errors import AIAuthenticationError, AIProviderError
from nexa_core.ai.models import AIEmbeddingModel, AIUsageEvent
from nexa_core.documents import embedding, indexing, vectors
from nexa_core.documents.models import (
    Document,
    DocumentChunk,
    DocumentEmbedding,
    DocumentIndex,
    DocumentText,
)
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization
from nexa_core.testing import PASSWORD, OrgFactory, UserFactory, add_member, indexed_document

pytestmark = pytest.mark.django_db
HANDBOOK = (
    "# Handbook\n\nWelcome to the team.\n\n## Refunds\n\nCustomers may request a refund "
    "within thirty days of purchase. Enterprise refunds need written approval.\n\n"
    "## Travel\n\nBook flights two weeks ahead."
)


@pytest.fixture
def pgvector() -> None:
    if not vectors.status(connection).ready:
        pytest.skip("needs PostgreSQL with the pgvector extension")


@pytest.fixture
def owner(make_user: UserFactory) -> User:
    return make_user("owner@example.com")


@pytest.fixture
def org(owner: User, make_org: OrgFactory) -> Organization:
    return make_org(owner)


def model() -> AIEmbeddingModel:
    active = embedding.active_model()
    assert active is not None
    return active


def state_of(document: Document, model_row: AIEmbeddingModel | None = None) -> DocumentEmbedding:
    return DocumentEmbedding.objects.get(document=document, embedding_model=model_row or model())


def vector_rows(document: Document, model_row: AIEmbeddingModel | None = None) -> list[Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, id, content_sha256, dimensions, round(vector_norm(embedding)::numeric, 4) "
            "FROM chunk_embeddings WHERE document_id = %s AND embedding_model_id = %s "
            "ORDER BY chunk_id",
            [document.id, (model_row or model()).id],
        )
        return list(cursor.fetchall())


def embedded(org: Organization, owner: User, text: str = HANDBOOK, **extra: Any) -> Document:
    """A document indexed and embedded (the embed task run directly, as the worker would)."""
    document: Document = indexed_document(org, owner, text, filename="handbook.md", **extra)
    state = state_of(document)
    assert embedding.run_embedding(document.id, model().id, state.generation) == "ready"
    return document


def reindex(document: Document) -> int:
    with transaction.atomic():
        index = indexing.request_indexing(document)
    assert indexing.run_indexing(document.id, index.generation) == "indexed"
    return state_of(document).generation


@pytest.mark.usefixtures("pgvector")
class TestRuns:
    def test_every_chunk_gets_a_unit_vector_and_usage_is_recorded(
        self, org: Organization, owner: User
    ) -> None:
        document = embedded(org, owner)
        chunks = DocumentChunk.objects.filter(document=document).count()
        rows = vector_rows(document)
        assert len(rows) == chunks == 3
        assert {row[3] for row in rows} == {model().dimensions}
        assert {float(row[4]) for row in rows} == {1.0}
        state = state_of(document)
        assert (state.status, state.chunk_count, state.embedded_count, state.reused_count) == (
            "ready",
            3,
            3,
            0,
        )
        assert (
            state.source_generation
            == DocumentIndex.objects.get(document=document).indexed_generation
        )
        usage = AIUsageEvent.objects.filter(feature="embedding", source_id=document.id)
        assert usage.count() == 1
        row = usage.get()
        assert (row.actor_type, row.status, row.alias) == ("system", "success", model().key)
        assert row.input_tokens > 0
        payload = embedding.semantic_payload(Document.objects.get(id=document.id))
        assert (payload["status"], payload["ready"], payload["model"]) == (
            "ready",
            True,
            model().key,
        )

    def test_reindexing_unchanged_text_reuses_every_vector(
        self, org: Organization, owner: User
    ) -> None:
        document = embedded(org, owner)
        before = vector_rows(document)
        generation = reindex(document)
        assert embedding.run_embedding(document.id, model().id, generation) == "ready"
        state = state_of(document)
        assert (state.embedded_count, state.reused_count) == (0, 3)
        assert vector_rows(document) == before  # same rows, untouched

    def test_changed_text_is_embedded_and_old_vectors_pruned(
        self, org: Organization, owner: User
    ) -> None:
        document = embedded(org, owner)
        old_chunks = set(
            DocumentChunk.objects.filter(document=document).values_list("id", flat=True)
        )
        changed = HANDBOOK.replace("thirty days", "sixty days")
        DocumentText.objects.filter(document=document).update(
            content=changed, char_count=len(changed)
        )
        generation = reindex(document)
        assert embedding.run_embedding(document.id, model().id, generation) == "ready"
        state = state_of(document)
        assert (state.embedded_count, state.reused_count) == (1, 2)
        current = set(DocumentChunk.objects.filter(document=document).values_list("id", flat=True))
        assert current != old_chunks
        assert {row[0] for row in vector_rows(document)} == current  # no stale vectors left

    def test_force_embeds_every_passage_again(self, org: Organization, owner: User) -> None:
        document = embedded(org, owner)
        with transaction.atomic():
            state = embedding.request_embedding(Document.objects.get(id=document.id), force=True)
        assert state is not None
        outcome = embedding.run_embedding(document.id, model().id, state.generation, force=True)
        assert outcome == "ready"
        assert (state_of(document).embedded_count, state_of(document).reused_count) == (3, 0)
        assert len(vector_rows(document)) == 3  # replaced, never duplicated

    def test_duplicate_and_superseded_runs_do_nothing(self, org: Organization, owner: User) -> None:
        document = embedded(org, owner)
        first = state_of(document).generation
        assert embedding.run_embedding(document.id, model().id, first) == "skipped"
        with transaction.atomic():
            newer = embedding.request_embedding(Document.objects.get(id=document.id))
        assert newer is not None
        assert embedding.run_embedding(document.id, model().id, first) == "superseded"
        assert embedding.run_embedding(document.id, model().id, newer.generation) == "ready"
        assert embedding.run_embedding(document.id, model().id, newer.generation) == "skipped"

    def test_a_partial_failure_keeps_finished_batches(
        self, org: Organization, owner: User, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.NEXA_EMBEDDING_BATCH_SIZE = 1
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        generation = state_of(document).generation
        real = embedding._embed
        calls = {"n": 0}

        def flaky(texts: Any, spec: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise AIProviderError(provider="mock", provider_model="m")
            return real(texts, spec)

        monkeypatch.setattr(embedding, "_embed", flaky)
        assert embedding.run_embedding(document.id, model().id, generation) == "retry"
        state = state_of(document)
        assert (state.status, state.error_code, state.retryable) == (
            "failed",
            "provider_unavailable",
            True,
        )
        assert len(vector_rows(document)) == 1  # the finished batch is kept
        assert embedding.run_embedding(document.id, model().id, generation) == "ready"
        state = state_of(document)
        assert (state.embedded_count, state.reused_count) == (2, 1)
        assert AIUsageEvent.objects.filter(feature="embedding", status="error").count() == 1

    def test_a_refused_request_is_not_retried(
        self, org: Organization, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")

        def refused(texts: Any, spec: Any) -> Any:
            raise AIAuthenticationError(provider="openai", provider_model="m")

        monkeypatch.setattr(embedding, "_embed", refused)
        outcome = embedding.run_embedding(document.id, model().id, state_of(document).generation)
        assert outcome == "failed"
        assert (state_of(document).error_code, state_of(document).retryable) == (
            "provider_error",
            False,
        )
        assert vector_rows(document) == []

    def test_invalid_vectors_store_nothing(
        self, org: Organization, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexa_ai.embeddings import providers

        broken = MockEmbeddingProvider(failures=["wrong_dimensions"])
        monkeypatch.setattr(providers, "MockEmbeddingProvider", lambda: broken)
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        outcome = embedding.run_embedding(document.id, model().id, state_of(document).generation)
        assert outcome == "failed"
        assert state_of(document).error_code == "provider_error"
        assert vector_rows(document) == []

    def test_deleting_a_document_removes_its_vectors(self, org: Organization, owner: User) -> None:
        document = embedded(org, owner)
        with transaction.atomic():
            Document.objects.filter(id=document.id).update(deleted_at=timezone.now())
            indexing.forget_index(document)
        assert vector_rows(document) == []
        assert not DocumentEmbedding.objects.filter(document=document).exists()

    def test_a_document_deleted_mid_run_keeps_no_vectors(
        self, org: Organization, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        real = embedding._embed

        def delete_then_embed(texts: Any, spec: Any) -> Any:
            with transaction.atomic():
                Document.objects.filter(id=document.id).update(deleted_at=timezone.now())
                indexing.forget_index(document)
            return real(texts, spec)

        monkeypatch.setattr(embedding, "_embed", delete_then_embed)
        outcome = embedding.run_embedding(document.id, model().id, state_of_any(document))
        assert outcome == "cancelled"
        assert vector_rows(document) == []

    def test_a_second_model_never_mixes_with_the_first(
        self, org: Organization, owner: User, settings: Any
    ) -> None:
        document = embedded(org, owner)
        first = model()
        second = AIEmbeddingModel.objects.create(
            key="nexa-embed-mock-wide",
            display_name="Wider mock",
            provider="mock",
            provider_model="mock-embed-hash",
            dimensions=128,
            version="hash-v2",
        )
        settings.NEXA_EMBEDDING_MODEL = second.key
        assert model().key == second.key
        with transaction.atomic():
            state = embedding.request_embedding(Document.objects.get(id=document.id))
        assert state is not None
        assert embedding.run_embedding(document.id, second.id, state.generation) == "ready"
        assert {row[3] for row in vector_rows(document, second)} == {128}
        assert {row[3] for row in vector_rows(document, first)} == {first.dimensions}
        assert len(vector_rows(document, first)) == len(vector_rows(document, second)) == 3

    def test_the_daily_budget(self, org: Organization, owner: User, settings: Any) -> None:
        settings.NEXA_EMBEDDING_ORG_TOKENS_PER_DAY = 1
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        outcome = embedding.run_embedding(document.id, model().id, state_of(document).generation)
        assert outcome == "failed"
        assert state_of(document).error_code == "quota_exceeded"
        assert vector_rows(document) == []

    def test_stalled_runs_are_failed(self, org: Organization, owner: User) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        DocumentEmbedding.objects.filter(document=document).update(
            status="embedding", started_at=timezone.now() - timedelta(days=1)
        )
        assert embedding.fail_stalled(timezone.now()) == 1
        state = state_of(document)
        assert (state.status, state.error_code, state.retryable) == ("failed", "timeout", True)


def state_of_any(document: Document) -> int:
    return DocumentEmbedding.objects.get(document=document).generation


class TestAvailability:
    def test_reasons(self, settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        settings.NEXA_EMBEDDING_ENABLED = False
        assert embedding.availability(fresh=True).reason == "disabled"
        settings.NEXA_EMBEDDING_ENABLED = True
        monkeypatch.setattr(
            vectors, "status", lambda conn: vectors.StoreStatus(False, False, False)
        )
        assert embedding.availability(fresh=True).reason == "pgvector_missing"

    def test_without_pgvector_nothing_is_queued_and_documents_still_work(
        self, org: Organization, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            vectors, "status", lambda conn: vectors.StoreStatus(False, False, False)
        )
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        assert DocumentIndex.objects.get(document=document).status == "indexed"
        assert not DocumentEmbedding.objects.filter(document=document).exists()
        payload = embedding.semantic_payload(document)
        assert (payload["status"], payload["reason"]) == ("unavailable", "pgvector_missing")

    def test_the_active_model_rule(self, settings: Any) -> None:
        settings.NEXA_AI_OPENAI_API_KEY = None
        settings.NEXA_EMBEDDING_MOCK_ALLOWED = True
        settings.NEXA_EMBEDDING_MODEL = ""
        assert model().key == "nexa-embed-mock"  # the default needs an OpenAI key
        settings.NEXA_AI_OPENAI_API_KEY = "sk-test"
        assert model().key == "nexa-embed-small"
        settings.NEXA_EMBEDDING_MODEL = "nexa-embed-mock"
        assert model().key == "nexa-embed-mock"
        settings.NEXA_AI_OPENAI_API_KEY = None
        settings.NEXA_EMBEDDING_MODEL = ""
        settings.NEXA_EMBEDDING_MOCK_ALLOWED = False
        assert model().key == "nexa-embed-small"
        assert embedding.availability(fresh=True).reason in (
            "provider_not_configured",
            "pgvector_missing",
        )


class TestRegistry:
    def test_space_fields_are_fixed(self) -> None:
        row = AIEmbeddingModel.objects.get(key="nexa-embed-mock")
        row.display_name = "Renamed"
        row.save()  # cosmetic fields may change
        row.dimensions = 512
        with pytest.raises(ValueError, match="fixed"):
            row.save()

    def test_one_default(self) -> None:
        with pytest.raises(IntegrityError):
            AIEmbeddingModel.objects.create(
                key="nexa-embed-other",
                display_name="Other",
                provider="mock",
                provider_model="m",
                dimensions=8,
                is_default=True,
            )


# --- API -----------------------------------------------------------------------------------------


def client_for(user: User) -> APIClient:
    api = APIClient()
    token = api.post(
        "/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json"
    ).data["data"]["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api


@pytest.mark.usefixtures("pgvector")
class TestApi:
    def test_document_payload_and_reembed(
        self,
        org: Organization,
        owner: User,
        django_capture_on_commit_callbacks: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexa_core.celery import app as celery_app

        monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
        document = embedded(org, owner)
        api = client_for(owner)
        semantic = api.get(f"/api/v1/documents/{document.id}/").data["data"]["index"]["semantic"]
        assert (semantic["status"], semantic["ready"]) == ("ready", True)
        with django_capture_on_commit_callbacks(execute=True):
            response = api.post(
                f"/api/v1/documents/{document.id}/index/", {"reembed": True}, format="json"
            )
        assert response.status_code == 202
        assert response.data["data"]["semantic"]["status"] in ("pending", "ready")
        state = state_of(document)
        assert (state.status, state.embedded_count) == ("ready", 3)
        bad = api.post(f"/api/v1/documents/{document.id}/index/", {"reembed": "yes"}, format="json")
        assert bad.status_code == 422

    def test_search_status_is_for_administrators(
        self, org: Organization, owner: User, make_user: UserFactory
    ) -> None:
        embedded(org, owner)
        member = make_user("member@example.com")
        add_member(org, member, "member")
        assert client_for(member).get("/api/v1/documents/search-status/").status_code == 403
        status = client_for(owner).get("/api/v1/documents/search-status/").data["data"]
        assert status["semantic"]["available"] is True
        assert status["semantic"]["model"]["key"] == model().key
        assert status["documents"]["semantic_ready"] == 1
        assert status["passages"] == {"total": 3, "embedded": 3}
        assert "vector" not in str(status["passages"])
