"""The search index on the Core side: indexing after extraction, idempotent and superseded
runs, re-indexing with stable chunk ids, failure that keeps the previous index, the index API
and its authorization, deletion, staleness, and the full-text column itself."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.contrib.postgres.search import SearchQuery
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from rest_framework.test import APIClient

from nexa_core.audit.models import AuditLog
from nexa_core.celery import app as celery_app
from nexa_core.documents import indexing
from nexa_core.documents.chunking import STRATEGY_VERSION
from nexa_core.documents.models import Document, DocumentChunk, DocumentIndex
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization
from nexa_core.testing import (
    PASSWORD,
    OrgFactory,
    UserFactory,
    add_member,
    indexed_document,
    make_pdf,
)

pytestmark = pytest.mark.django_db
URL = "/api/v1/documents/"
HANDBOOK = (
    "# Handbook\n\nWelcome to the team.\n\n## Refunds\n\nCustomers may request a refund "
    "within thirty days of purchase. Enterprise refunds need written approval.\n\n"
    "## Travel\n\nBook flights two weeks ahead."
)


@pytest.fixture(autouse=True)
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


@pytest.fixture(autouse=True)
def storage(settings: Any, tmp_path: Path) -> Path:
    settings.NEXA_DOCUMENTS_STORAGE_ROOT = tmp_path / "documents"
    return tmp_path


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Callable[[], Any]:
    return lambda: django_capture_on_commit_callbacks(execute=True)


@pytest.fixture
def owner(make_user: UserFactory) -> User:
    return make_user("owner@example.com")


@pytest.fixture
def org(owner: User, make_org: OrgFactory) -> Organization:
    return make_org(owner)


@pytest.fixture
def member(org: Organization, make_user: UserFactory) -> User:
    user = make_user("member@example.com")
    add_member(org, user, "member")
    return user


def client_for(user: User) -> APIClient:
    api = APIClient()
    token = api.post(
        "/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json"
    ).data["data"]["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api


@pytest.fixture
def api(member: User) -> APIClient:
    return client_for(member)


def upload(api: APIClient, commit: Callable[[], Any], name: str, data: bytes, **fields: Any) -> Any:
    with commit():
        response = api.post(
            URL, {"file": SimpleUploadedFile(name, data), **fields}, format="multipart"
        )
    assert response.status_code == 201, response.data
    return api.get(f"{URL}{response.data['data']['id']}/").data["data"]


def chunk_ids(document_id: Any) -> list[str]:
    return [
        str(i)
        for i in DocumentChunk.objects.filter(document_id=document_id)
        .order_by("chunk_index")
        .values_list("id", flat=True)
    ]


class TestIndexingAfterExtraction:
    def test_a_processed_document_is_indexed(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = upload(api, commit, "handbook.md", HANDBOOK.encode())
        index = document["index"]
        assert index["status"] == "indexed"
        assert index["searchable"] is True
        assert index["chunk_count"] == 3
        assert index["strategy_version"] == STRATEGY_VERSION
        assert document["can_reindex"] is True
        chunks = list(DocumentChunk.objects.filter(document_id=document["id"]))
        assert [c.section for c in chunks] == [
            "Handbook",
            "Handbook > Refunds",
            "Handbook > Travel",
        ]
        assert {c.organization_id for c in chunks} == {
            Document.objects.get(id=document["id"]).organization_id
        }
        status = api.get(f"{URL}{document['id']}/index-status/").data["data"]
        assert status == index

    def test_pdf_chunks_carry_their_pages(self, api: APIClient, commit: Callable[[], Any]) -> None:
        document = upload(
            api, commit, "two.pdf", make_pdf(["Refund rules live here.", "Travel rules live here."])
        )
        chunks = DocumentChunk.objects.filter(document_id=document["id"]).order_by("chunk_index")
        assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]

    def test_a_failed_extraction_is_not_indexed(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = upload(api, commit, "scan.pdf", make_pdf([None]))
        assert document["status"] == "needs_ocr"
        assert document["index"]["status"] == "none"
        assert document["can_reindex"] is False

    def test_a_document_without_text_fails_to_index_with_a_reason(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = upload(api, commit, "blank.txt", b"   \n\n  ")
        assert document["status"] == "completed"
        assert document["index"]["status"] == "failed"
        assert document["index"]["error_code"] == "no_text"
        assert document["index"]["searchable"] is False


class TestRuns:
    def test_a_duplicate_run_does_nothing(self, org: Organization, owner: User) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        before = chunk_ids(document.id)
        assert indexing.run_indexing(document.id, 1) == "skipped"
        assert chunk_ids(document.id) == before

    def test_an_older_generation_is_superseded(self, org: Organization, owner: User) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md", index=False)
        DocumentIndex.objects.create(document=document, organization=org, generation=2)
        assert indexing.run_indexing(document.id, 1) == "superseded"
        assert not DocumentChunk.objects.filter(document=document).exists()
        assert indexing.run_indexing(document.id, 2) == "indexed"

    def test_reindexing_unchanged_text_keeps_chunk_ids(
        self, org: Organization, owner: User
    ) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        before = chunk_ids(document.id)
        with transaction.atomic():
            index = indexing.request_indexing(document)
        assert indexing.run_indexing(document.id, index.generation) == "indexed"
        assert chunk_ids(document.id) == before
        assert DocumentIndex.objects.get(document=document).indexed_generation == index.generation

    def test_a_failed_reindex_keeps_the_previous_chunks(
        self, org: Organization, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        before = chunk_ids(document.id)

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("chunker bug")

        monkeypatch.setattr(indexing, "chunk_text", broken)
        with transaction.atomic():
            index = indexing.request_indexing(document)
        assert indexing.run_indexing(document.id, index.generation) == "failed"
        index.refresh_from_db()
        assert (index.status, index.error_code) == ("failed", "indexing_failed")
        assert index.searchable is True  # the previous run's chunks still serve
        assert chunk_ids(document.id) == before
        assert indexing.index_payload(index)["status"] == "failed"

    def test_a_run_for_a_deleted_document_is_cancelled(
        self, org: Organization, owner: User
    ) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md", index=False)
        DocumentIndex.objects.create(document=document, organization=org, generation=1)
        Document.objects.filter(id=document.id).update(deleted_at=document.created_at)
        assert indexing.run_indexing(document.id, 1) == "cancelled"

    def test_a_live_run_is_not_taken_over_but_a_dead_one_is(
        self, org: Organization, owner: User
    ) -> None:
        from datetime import timedelta

        from django.utils import timezone

        document = indexed_document(org, owner, HANDBOOK, filename="h.md", index=False)
        index = DocumentIndex.objects.create(
            document=document,
            organization=org,
            generation=1,
            status="indexing",
            started_at=timezone.now(),
        )
        assert indexing.run_indexing(document.id, 1) == "skipped"
        index.started_at = timezone.now() - timedelta(hours=1)
        index.save()
        assert indexing.fail_stalled(timezone.now()) == 1
        index.refresh_from_db()
        assert (index.status, index.error_code) == ("failed", "timeout")

    def test_stale_strategy_is_reported(self, org: Organization, owner: User) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        DocumentIndex.objects.filter(document=document).update(strategy_version="old/v0")
        payload = indexing.index_payload(DocumentIndex.objects.get(document=document))
        assert (payload["status"], payload["stale"], payload["searchable"]) == ("stale", True, True)

    def test_full_text_search_column(self, org: Organization, owner: User) -> None:
        document = indexed_document(org, owner, HANDBOOK, filename="h.md")
        found = DocumentChunk.objects.filter(
            document=document, search=SearchQuery("refunds", config="english")
        )
        assert [c.section for c in found] == ["Handbook > Refunds"]
        by_heading = DocumentChunk.objects.filter(
            document=document, search=SearchQuery("travel", config="english")
        )
        assert [c.section for c in by_heading] == ["Handbook > Travel"]


class TestIndexApi:
    def test_reindex_request(self, api: APIClient, commit: Callable[[], Any]) -> None:
        document = upload(api, commit, "handbook.md", HANDBOOK.encode())
        before = chunk_ids(document["id"])
        with commit():
            response = api.post(f"{URL}{document['id']}/index/")
        assert response.status_code == 202
        assert response.data["data"]["status"] in ("pending", "indexed")
        after = api.get(f"{URL}{document['id']}/index-status/").data["data"]
        assert after["status"] == "indexed"
        assert chunk_ids(document["id"]) == before
        assert AuditLog.objects.filter(action="DOCUMENT_INDEX_REQUESTED").count() == 1

    def test_reindex_needs_a_processed_document(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = upload(api, commit, "scan.pdf", make_pdf([None]))
        assert api.post(f"{URL}{document['id']}/index/").status_code == 409

    def test_reindex_is_rate_limited(
        self, api: APIClient, commit: Callable[[], Any], settings: Any
    ) -> None:
        settings.NEXA_RAG_INDEX_REQUESTS_PER_HOUR = 1
        document = upload(api, commit, "a.txt", b"some text")
        assert api.post(f"{URL}{document['id']}/index/").status_code == 202
        assert api.post(f"{URL}{document['id']}/index/").status_code == 429

    def test_others_cannot_reindex_or_read_index_state(
        self,
        api: APIClient,
        commit: Callable[[], Any],
        org: Organization,
        make_user: UserFactory,
    ) -> None:
        private = upload(api, commit, "private.txt", b"secret text")
        shared = upload(api, commit, "shared.txt", b"shared text", visibility="organization")
        colleague = make_user("colleague@example.com")
        add_member(org, colleague, "member")
        other = client_for(colleague)
        assert other.get(f"{URL}{private['id']}/index-status/").status_code == 404
        assert other.post(f"{URL}{private['id']}/index/").status_code == 404
        assert other.get(f"{URL}{shared['id']}/index-status/").status_code == 200
        assert other.post(f"{URL}{shared['id']}/index/").status_code == 403

    def test_the_searchable_filter(self, api: APIClient, commit: Callable[[], Any]) -> None:
        ready = upload(api, commit, "ready.txt", b"indexed text")
        upload(api, commit, "scan.pdf", make_pdf([None]))
        listed = api.get(URL, {"searchable": "true"}).data["data"]
        assert [d["id"] for d in listed] == [ready["id"]]
        assert [d["filename"] for d in api.get(URL, {"searchable": "false"}).data["data"]] == [
            "scan.pdf"
        ]
        assert api.get(URL, {"searchable": "maybe"}).status_code == 422

    def test_deleting_a_document_removes_its_chunks_and_index(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = upload(api, commit, "handbook.md", HANDBOOK.encode())
        assert DocumentChunk.objects.filter(document_id=document["id"]).exists()
        assert api.delete(f"{URL}{document['id']}/").status_code == 200
        assert not DocumentChunk.objects.filter(document_id=document["id"]).exists()
        assert not DocumentIndex.objects.filter(document_id=document["id"]).exists()
