"""The documents API end to end: upload validation, processing through the (eager) worker,
reading the text, sharing, retries, deletion, and who may see or touch what.

Files are synthetic and built in memory; storage is a temporary directory per test.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from nexa_core.audit.models import AuditLog
from nexa_core.celery import app as celery_app
from nexa_core.documents.models import Document, DocumentIngestion, DocumentText
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization, Team, TeamMembership
from nexa_core.testing import PASSWORD, OrgFactory, UserFactory, add_member, make_docx, make_pdf

pytestmark = pytest.mark.django_db
URL = "/api/v1/documents/"
SettingsWrapper = Any  # pytest-django's `settings` fixture


@pytest.fixture(autouse=True)
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


@pytest.fixture(autouse=True)
def storage(settings: SettingsWrapper, tmp_path: Path) -> Path:
    root = tmp_path / "documents"
    settings.NEXA_DOCUMENTS_STORAGE_ROOT = root
    return root


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Callable[[], Any]:
    """Run on-commit callbacks (the processing task) as if the transaction had committed."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def client_in(user: User, org: Organization) -> APIClient:
    """A client signed in as `user` with `org` as the active organization."""
    api = APIClient()
    response = api.post(
        "/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200, response.data
    token = response.data["data"]["access_token"]
    if response.data["data"]["active_organization_id"] != str(org.id):
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        switched = api.post(
            "/api/v1/auth/switch-organization/", {"organization_id": str(org.id)}, format="json"
        )
        assert switched.status_code == 200, switched.data
        token = switched.data["data"]["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api


def upload(
    api: APIClient,
    name: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    **fields: Any,
) -> Any:
    payload = {"file": SimpleUploadedFile(name, data, content_type=content_type), **fields}
    return api.post(URL, payload, format="multipart")


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


@pytest.fixture
def api(member: User, org: Organization) -> APIClient:
    return client_in(member, org)


def processed(
    api: APIClient, commit: Callable[[], Any], name: str, data: bytes, **fields: Any
) -> Any:
    with commit():
        response = upload(api, name, data, **fields)
    assert response.status_code == 201, response.data
    return api.get(f"{URL}{response.data['data']['id']}/").data["data"]


class TestUpload:
    def test_a_text_file_is_stored_processed_and_readable(
        self, api: APIClient, commit: Callable[[], Any], storage: Path, org: Organization
    ) -> None:
        with commit():
            response = upload(api, "notes.txt", b"Hello documents.\nSecond line.", title="Notes")
        assert response.status_code == 201
        body = response.data["data"]
        assert body["status"] == "queued"
        assert body["filename"] == "notes.txt"
        assert body["format"] == "txt"
        assert "storage_key" not in body
        assert str(storage) not in str(response.data)

        document = api.get(f"{URL}{body['id']}/").data["data"]
        assert document["status"] == "completed"
        assert document["has_text"] is True
        assert document["word_count"] == 4
        assert [i["status"] for i in document["ingestions"]] == ["completed"]
        stored = list((storage / str(org.id)).iterdir())
        assert [p.name for p in stored] == [f"{body['id']}.txt"]

        content = api.get(f"{URL}{body['id']}/content/").data["data"]
        assert content["content"] == "Hello documents.\nSecond line."
        assert content["has_more"] is False
        assert AuditLog.objects.filter(
            action="DOCUMENT_UPLOADED", resource_id=body["id"], organization_id=org.id
        ).exists()

    def test_the_client_content_type_is_ignored(self, api: APIClient) -> None:
        response = upload(api, "notes.txt", b"plain text", content_type="application/pdf")
        assert response.status_code == 201
        assert response.data["data"]["format"] == "txt"
        assert response.data["data"]["media_type"] == "text/plain"

    def test_content_that_does_not_match_the_extension_is_refused(self, api: APIClient) -> None:
        response = upload(api, "report.pdf", b"this is not a pdf", content_type="application/pdf")
        assert response.status_code == 415
        assert response.data["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
        docx_named_txt = upload(api, "notes.txt", make_docx())
        assert docx_named_txt.status_code == 415
        assert Document.objects.count() == 0

    def test_unsupported_extensions_are_refused(self, api: APIClient) -> None:
        for name in ("tool.exe", "page.html", "noextension"):
            response = upload(api, name, b"MZ binary")
            assert response.status_code == 415, name

    def test_size_limit(self, api: APIClient, settings: SettingsWrapper) -> None:
        settings.NEXA_DOCUMENTS_MAX_UPLOAD_BYTES = 10
        response = upload(api, "big.txt", b"x" * 11)
        assert response.status_code == 413
        assert response.data["error"]["code"] == "PAYLOAD_TOO_LARGE"

    def test_empty_and_missing_files(self, api: APIClient) -> None:
        assert upload(api, "empty.txt", b"").status_code == 422
        assert api.post(URL, {"title": "no file"}, format="multipart").status_code == 422

    def test_filenames_are_sanitized_and_never_become_paths(
        self, api: APIClient, storage: Path, org: Organization
    ) -> None:
        response = upload(api, "../../etc/pass\x07wd.txt", b"data")
        assert response.status_code == 201
        assert response.data["data"]["filename"] == "passwd.txt"
        windows = upload(api, "C:\\Users\\x\\..\\report.md", b"# Title")
        assert windows.data["data"]["filename"] == "report.md"
        files = sorted(p.name for p in (storage / str(org.id)).iterdir())
        assert files == sorted(
            [f"{response.data['data']['id']}.txt", f"{windows.data['data']['id']}.md"]
        )

    def test_organization_storage_quota(self, api: APIClient, settings: SettingsWrapper) -> None:
        settings.NEXA_DOCUMENTS_ORG_STORAGE_BYTES = 15
        assert upload(api, "a.txt", b"0123456789").status_code == 201
        response = upload(api, "b.txt", b"0123456789")
        assert response.status_code == 402
        assert response.data["error"]["code"] == "QUOTA_EXCEEDED"

    def test_upload_rate_limit(self, api: APIClient, settings: SettingsWrapper) -> None:
        settings.NEXA_DOCUMENTS_UPLOADS_PER_HOUR = 1
        assert upload(api, "a.txt", b"one").status_code == 201
        response = upload(api, "b.txt", b"two")
        assert response.status_code == 429
        assert response.data["error"]["code"] == "RATE_LIMITED"

    def test_viewers_cannot_upload(self, org: Organization, make_user: UserFactory) -> None:
        viewer = make_user("viewer@example.com")
        add_member(org, viewer, "viewer")
        response = upload(client_in(viewer, org), "a.txt", b"text")
        assert response.status_code == 403

    def test_invalid_fields(self, api: APIClient) -> None:
        bad = upload(api, "a.txt", b"x", category="Not A Slug")
        assert bad.status_code == 422
        team = upload(api, "a.txt", b"x", visibility="team")
        assert team.status_code == 422  # a team is required
        assert upload(api, "a.txt", b"x", visibility="everyone").status_code == 422


class TestProcessing:
    def test_pdf_pages_are_kept(self, api: APIClient, commit: Callable[[], Any]) -> None:
        document = processed(api, commit, "two.pdf", make_pdf(["First page", "Second page"]))
        assert document["status"] == "completed"
        assert document["page_count"] == 2
        page = api.get(f"{URL}{document['id']}/content/?page=2").data["data"]
        assert page["content"] == "Second page"
        assert page["pages"] == [{"page": 2, "char_start": 12, "char_end": 23}]
        assert api.get(f"{URL}{document['id']}/content/?page=3").status_code == 404

    def test_docx_is_processed(self, api: APIClient, commit: Callable[[], Any]) -> None:
        document = processed(api, commit, "notes.docx", make_docx())
        assert document["status"] == "completed"
        assert document["metadata"]["title"] == "Platform notes"
        assert "formula_cells_neutralized" in document["warnings"]

    def test_an_image_only_pdf_needs_ocr_and_can_be_retried(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = processed(api, commit, "scan.pdf", make_pdf([None, None]))
        assert document["status"] == "needs_ocr"
        assert document["error_code"] == "needs_ocr"
        assert document["can_retry"] is True
        assert api.get(f"{URL}{document['id']}/content/").status_code == 409

        with commit():
            retried = api.post(f"{URL}{document['id']}/retry/")
        assert retried.status_code == 202
        again = api.get(f"{URL}{document['id']}/").data["data"]
        assert again["attempt_count"] == 2
        assert [i["attempt"] for i in again["ingestions"]] == [2, 1]
        assert again["status"] == "needs_ocr"
        assert AuditLog.objects.filter(action="DOCUMENT_RETRIED").count() == 1

    def test_a_completed_document_cannot_be_retried(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = processed(api, commit, "a.txt", b"text")
        response = api.post(f"{URL}{document['id']}/retry/")
        assert response.status_code == 409

    def test_retries_are_capped(
        self, api: APIClient, commit: Callable[[], Any], settings: SettingsWrapper
    ) -> None:
        settings.NEXA_DOCUMENTS_MAX_ATTEMPTS = 1
        document = processed(api, commit, "scan.pdf", make_pdf([None]))
        assert api.post(f"{URL}{document['id']}/retry/").status_code == 409

    def test_a_corrupted_file_fails_with_a_safe_message(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = processed(api, commit, "broken.pdf", b"%PDF-1.4\n garbage without objects")
        assert document["status"] == "failed"
        assert document["error_code"] == "corrupted"
        assert "Traceback" not in (document["error_message"] or "")

    def test_content_is_unavailable_until_processed(self, api: APIClient) -> None:
        response = upload(api, "a.txt", b"text")  # on-commit callbacks never run here
        document_id = response.data["data"]["id"]
        assert api.get(f"{URL}{document_id}/content/").status_code == 409
        status = api.get(f"{URL}{document_id}/status/").data["data"]
        assert status["status"] == "queued"


class TestReading:
    def test_content_slices(self, api: APIClient, commit: Callable[[], Any]) -> None:
        document = processed(api, commit, "a.txt", b"abcdefghij")
        first = api.get(f"{URL}{document['id']}/content/?offset=0&limit=4").data["data"]
        assert (first["content"], first["next_offset"], first["has_more"]) == ("abcd", 4, True)
        last = api.get(f"{URL}{document['id']}/content/?offset=8&limit=4").data["data"]
        assert (last["content"], last["next_offset"], last["has_more"]) == ("ij", None, False)

    def test_preview_is_plain_text_never_html(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = processed(api, commit, "page.md", b"# Hi\n<script>alert(1)</script>")
        response = api.get(f"{URL}{document['id']}/preview/")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/plain; charset=utf-8"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cache-Control"] == "private, no-store"
        assert b"<script>alert(1)</script>" in response.content

    def test_download_returns_the_original_bytes_as_an_attachment(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        data = make_pdf(["Original"])
        document = processed(api, commit, "orig.pdf", data)
        response: Any = api.get(f"{URL}{document['id']}/download/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == data
        assert response["Content-Disposition"].startswith("attachment;")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert AuditLog.objects.filter(action="DOCUMENT_DOWNLOADED").count() == 1


class TestLibrary:
    def test_filters_search_and_pagination(self, api: APIClient, commit: Callable[[], Any]) -> None:
        processed(api, commit, "alpha.txt", b"one", category="notes")
        processed(api, commit, "beta.md", b"# two")
        processed(api, commit, "scan.pdf", make_pdf([None]))

        def names(response: Any) -> list[str]:
            return [d["filename"] for d in response.data["data"]]

        assert names(api.get(URL)) == ["scan.pdf", "beta.md", "alpha.txt"]
        assert names(api.get(URL, {"status": "needs_ocr"})) == ["scan.pdf"]
        assert names(api.get(URL, {"format": "md"})) == ["beta.md"]
        assert names(api.get(URL, {"category": "notes"})) == ["alpha.txt"]
        assert names(api.get(URL, {"q": "ALP"})) == ["alpha.txt"]
        assert names(api.get(URL, {"owner": "me"})) == ["scan.pdf", "beta.md", "alpha.txt"]
        assert api.get(URL, {"status": "nope"}).status_code == 422
        assert api.get(URL, {"cursor": "not-a-uuid"}).status_code == 422

        first = api.get(URL, {"limit": 2})
        assert names(first) == ["scan.pdf", "beta.md"]
        assert first.data["meta"]["has_more"] is True
        second = api.get(URL, {"limit": 2, "cursor": first.data["meta"]["next_cursor"]})
        assert names(second) == ["alpha.txt"]
        assert second.data["meta"]["has_more"] is False
        assert first.data["meta"]["limits"]["max_upload_bytes"] > 0
        assert ".pdf" in first.data["meta"]["limits"]["accepted_extensions"]

    def test_update_title_category_and_visibility(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        document = processed(api, commit, "a.txt", b"text")
        response = api.patch(
            f"{URL}{document['id']}/",
            {"title": "Renamed", "category": "policies", "visibility": "organization"},
            format="json",
        )
        assert response.status_code == 200
        body = response.data["data"]
        assert (body["title"], body["category"], body["visibility"]) == (
            "Renamed",
            "policies",
            "organization",
        )
        assert AuditLog.objects.filter(action="DOCUMENT_UPDATED").count() == 1
        bad = api.patch(f"{URL}{document['id']}/", {"category": "Bad Slug"}, format="json")
        assert bad.status_code == 422

    def test_delete_removes_the_file_and_the_text(
        self, api: APIClient, commit: Callable[[], Any], storage: Path, org: Organization
    ) -> None:
        document = processed(api, commit, "a.txt", b"text")
        response = api.delete(f"{URL}{document['id']}/")
        assert response.status_code == 200
        assert api.get(f"{URL}{document['id']}/").status_code == 404
        assert api.get(URL).data["data"] == []
        assert not any((storage / str(org.id)).iterdir())
        row = Document.objects.get(id=document["id"])
        assert row.deleted_at is not None
        assert row.file_deleted_at is not None
        assert not DocumentText.objects.filter(document_id=document["id"]).exists()
        assert AuditLog.objects.filter(action="DOCUMENT_DELETED").count() == 1


class TestAuthorization:
    @pytest.fixture
    def colleague(self, org: Organization, make_user: UserFactory) -> User:
        user = make_user("colleague@example.com")
        add_member(org, user, "member")
        return user

    def test_private_documents_are_invisible_to_colleagues(
        self, api: APIClient, commit: Callable[[], Any], colleague: User, org: Organization
    ) -> None:
        document = processed(api, commit, "private.txt", b"secret")
        other = client_in(colleague, org)
        assert other.get(URL).data["data"] == []
        for suffix in ("", "status/", "content/", "preview/", "download/"):
            assert other.get(f"{URL}{document['id']}/{suffix}").status_code == 404, suffix
        assert other.post(f"{URL}{document['id']}/retry/").status_code == 404
        assert other.delete(f"{URL}{document['id']}/").status_code == 404

    def test_organization_documents_are_readable_but_not_editable_by_colleagues(
        self, api: APIClient, commit: Callable[[], Any], colleague: User, org: Organization
    ) -> None:
        document = processed(api, commit, "shared.txt", b"shared", visibility="organization")
        other = client_in(colleague, org)
        assert [d["id"] for d in other.get(URL).data["data"]] == [document["id"]]
        listed = other.get(URL).data["data"][0]
        assert (listed["editable"], listed["deletable"]) == (False, False)
        assert other.get(f"{URL}{document['id']}/content/").status_code == 200
        patch = other.patch(f"{URL}{document['id']}/", {"title": "mine"}, format="json")
        assert patch.status_code == 403
        assert other.delete(f"{URL}{document['id']}/").status_code == 403

    def test_team_documents(
        self,
        api: APIClient,
        commit: Callable[[], Any],
        member: User,
        colleague: User,
        org: Organization,
        make_user: UserFactory,
    ) -> None:
        team = Team.objects.create(organization=org, name="Research", slug="research")
        for user in (member, colleague):
            TeamMembership.objects.create(
                organization=org,
                team=team,
                membership=user.memberships.get(organization=org),
            )
        outsider = make_user("outsider@example.com")
        add_member(org, outsider, "member")
        # Refresh the principal: team membership is read when the token is used.
        api = client_in(member, org)
        document = processed(
            api, commit, "team.txt", b"team", visibility="team", team_id=str(team.id)
        )
        assert document["team_id"] == str(team.id)
        assert client_in(colleague, org).get(f"{URL}{document['id']}/").status_code == 200
        assert client_in(outsider, org).get(f"{URL}{document['id']}/").status_code == 404

        other_team = Team.objects.create(organization=org, name="Ops", slug="ops")
        response = upload(api, "x.txt", b"x", visibility="team", team_id=str(other_team.id))
        assert response.status_code == 404  # not a team of the uploader

    def test_admins_manage_every_document(
        self, api: APIClient, commit: Callable[[], Any], org: Organization, owner: User
    ) -> None:
        document = processed(api, commit, "private.txt", b"secret")
        admin = client_in(owner, org)
        assert [d["id"] for d in admin.get(URL).data["data"]] == [document["id"]]
        assert admin.delete(f"{URL}{document['id']}/").status_code == 200

    def test_other_organizations_see_nothing(
        self,
        api: APIClient,
        commit: Callable[[], Any],
        make_user: UserFactory,
        make_org: OrgFactory,
    ) -> None:
        document = processed(api, commit, "shared.txt", b"shared", visibility="organization")
        stranger = make_user("stranger@example.com")
        their_org = make_org(stranger, name="Elsewhere")
        other = client_in(stranger, their_org)
        assert other.get(URL).data["data"] == []
        assert other.get(f"{URL}{document['id']}/").status_code == 404
        assert other.get(f"{URL}{document['id']}/download/").status_code == 404
        missing = other.get(f"{URL}{uuid4()}/")
        foreign = other.get(f"{URL}{document['id']}/")
        assert missing.data["error"] == foreign.data["error"]

    def test_a_conversation_link_must_be_your_own_conversation(
        self, api: APIClient, colleague: User, org: Organization
    ) -> None:
        from nexa_core.chat.models import Conversation

        theirs = Conversation.objects.create(organization=org, owner=colleague)
        response = upload(api, "a.txt", b"x", conversation_id=str(theirs.id))
        assert response.status_code == 404

    def test_requests_without_an_organization_are_refused(self, make_user: UserFactory) -> None:
        loner = make_user("loner@example.com")
        api = APIClient()
        token = api.post(
            "/api/v1/auth/login/", {"email": loner.email, "password": PASSWORD}, format="json"
        ).data["data"]["access_token"]
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        assert api.get(URL).status_code == 403
        assert APIClient().get(URL).status_code == 401


class TestWorker:
    def test_a_duplicate_task_does_nothing(self, api: APIClient, commit: Callable[[], Any]) -> None:
        from nexa_core.documents.tasks import run_processing

        document = processed(api, commit, "a.txt", b"text")
        ingestion = DocumentIngestion.objects.get(document_id=document["id"])
        assert run_processing(ingestion.document_id, ingestion.id) == "skipped"
        assert DocumentIngestion.objects.filter(document_id=document["id"]).count() == 1

    def test_a_task_for_a_mismatched_document_does_nothing(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        from nexa_core.documents.tasks import run_processing

        first = upload(api, "a.txt", b"one").data["data"]
        second = upload(api, "b.txt", b"two").data["data"]
        ingestion = DocumentIngestion.objects.get(document_id=first["id"])
        assert run_processing(Document.objects.get(id=second["id"]).id, ingestion.id) == "skipped"
        assert Document.objects.get(id=first["id"]).status == "queued"

    def test_a_document_deleted_before_processing_is_cancelled(self, api: APIClient) -> None:
        from nexa_core.documents.tasks import run_processing

        body = upload(api, "a.txt", b"text").data["data"]
        assert api.delete(f"{URL}{body['id']}/").status_code == 200
        ingestion = DocumentIngestion.objects.get(document_id=body["id"])
        # Deleting cancels the queued attempt, so a late task finds nothing to do.
        assert ingestion.status == "cancelled"
        assert run_processing(ingestion.document_id, ingestion.id) == "skipped"
        assert not DocumentText.objects.filter(document_id=body["id"]).exists()

    def test_the_broker_being_down_processes_inline(
        self, api: APIClient, commit: Callable[[], Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexa_core.documents import queue
        from nexa_core.documents.tasks import index_document, process_document

        def unavailable(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("broker down")

        monkeypatch.setattr(process_document, "apply_async", unavailable)
        monkeypatch.setattr(index_document, "apply_async", unavailable)
        monkeypatch.setattr(queue, "_broker_down_until", 0.0)
        with commit():
            body = upload(api, "a.txt", b"text").data["data"]
        assert Document.objects.get(id=body["id"]).status == "completed"
        document = api.get(f"{URL}{body['id']}/").data["data"]
        assert document["index"]["status"] == "indexed"

    def test_purge_finishes_deletions_and_fails_stalled_attempts(
        self,
        api: APIClient,
        commit: Callable[[], Any],
        settings: SettingsWrapper,
        storage: Path,
        org: Organization,
    ) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from nexa_core.documents.tasks import purge_documents

        gone = processed(api, commit, "gone.txt", b"bye")
        api.delete(f"{URL}{gone['id']}/")
        Document.objects.filter(id=gone["id"]).update(
            deleted_at=timezone.now() - timedelta(days=settings.NEXA_DOCUMENTS_PURGE_AFTER_DAYS + 1)
        )
        stuck = upload(api, "stuck.txt", b"text").data["data"]  # queued, never processed
        Document.objects.filter(id=stuck["id"]).update(
            updated_at=timezone.now() - timedelta(days=1)
        )
        counts = purge_documents()
        assert counts == {
            "files_removed": 0,
            "purged": 1,
            "stalled": 1,
            "indexes_stalled": 0,
            "embeddings_stalled": 0,
        }
        assert not Document.objects.filter(id=gone["id"]).exists()
        stalled = Document.objects.get(id=stuck["id"])
        assert (stalled.status, stalled.error_code) == ("failed", "timeout")
        assert api.get(f"{URL}{stuck['id']}/").data["data"]["can_retry"] is True


class TestStorage:
    def test_keys_cannot_escape_the_root(self, storage: Path) -> None:
        from nexa_core.documents.storage import LocalDocumentStorage, StorageError

        backend = LocalDocumentStorage(storage)
        for key in ("../x.txt", "a/../../x.txt", "/abs/x.txt", "a\\b.txt", "x.txt", ""):
            with pytest.raises(StorageError):
                backend.save(key, iter([b"x"]))

    def test_existing_files_are_never_overwritten(self, storage: Path) -> None:
        from nexa_core.documents.storage import LocalDocumentStorage, StorageError

        backend = LocalDocumentStorage(storage)
        assert backend.save("org/doc.txt", iter([b"one"])) == 3
        with pytest.raises(StorageError):
            backend.save("org/doc.txt", iter([b"two"]))
        with backend.open("org/doc.txt") as handle:
            assert handle.read() == b"one"
