"""The agents API (Phase 11): agent definitions in Core, who may see and change them, and the
checks every configuration passes before it is saved (docs/api/agents.md#agents).

Runs (tasks) are Runtime's and are tested there (apps/runtime/tests/test_agents_runs.py).
"""

from typing import Any

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from nexa_common.tool_catalog import BUILTIN_TOOLS, TOOLS_BY_ID
from nexa_core.agents.catalog import DOCUMENT_ASSISTANT_SLUG, sync_agent_catalog
from nexa_core.agents.models import Agent
from nexa_core.ai.models import AIModelAlias, AIOrganizationAliasGrant
from nexa_core.audit.models import AuditLog
from nexa_core.identity.models import User
from nexa_core.organizations.models import Organization, RolePermission
from nexa_core.testing import PASSWORD, OrgFactory, UserFactory, add_member
from nexa_core.tools.models import ToolSetting

pytestmark = pytest.mark.django_db
URL = "/api/v1/agents/"

INSTRUCTIONS = "Answer questions about our refund policy. Search the documents first."


def client_in(user: User, org: Organization) -> APIClient:
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


@pytest.fixture
def owner(make_user: UserFactory) -> User:
    return make_user("owner@example.com")


@pytest.fixture
def org(owner: User, make_org: OrgFactory) -> Organization:
    return make_org(owner)


def joined(org: Organization, make_user: UserFactory, email: str, role: str) -> User:
    user = make_user(email)
    add_member(org, user, role)
    return user


def body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "Refund helper",
        "description": "Answers refund questions.",
        "instructions": INSTRUCTIONS,
        "allowed_tools": ["search_documents"],
    }
    values.update(overrides)
    return values


def created(api: APIClient, **overrides: Any) -> dict[str, Any]:
    response = api.post(URL, body(**overrides), format="json")
    assert response.status_code == 201, response.data
    data: dict[str, Any] = response.data["data"]
    return data


def error_code(response: Any) -> str:
    return str(response.data["error"]["code"])


# --- Reading -------------------------------------------------------------------------------------


class TestListing:
    def test_members_see_the_platform_agents_and_their_organizations(
        self, owner: User, org: Organization, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        viewer = joined(org, make_user, "viewer@example.com", "viewer")
        mine = created(client_in(owner, org))
        stranger = make_user("stranger@example.com")
        other = make_org(stranger, name="Other")
        theirs = created(client_in(stranger, other), name="Their helper")

        listing = client_in(viewer, org).get(URL)
        assert listing.status_code == 200, listing.data
        ids = [a["id"] for a in listing.data["data"]]
        assistant = Agent.objects.get(slug=DOCUMENT_ASSISTANT_SLUG, organization=None)
        assert ids[0] == str(assistant.id)  # platform agents first
        assert mine["id"] in ids
        assert theirs["id"] not in ids
        first = listing.data["data"][0]
        assert (first["scope"], first["is_system"], first["can_edit"]) == ("platform", True, False)
        assert "instructions" not in first  # the list is a summary
        assert {t["id"] for t in first["tools"]} == {"search_documents", "document_metadata"}

        detail = client_in(viewer, org).get(f"{URL}{assistant.id}/")
        assert detail.status_code == 200
        assert detail.data["data"]["output_schema"]["required"] == ["answer", "answered"]
        assert client_in(viewer, org).get(f"{URL}{theirs['id']}/").status_code == 404

    def test_signing_in_is_required(self) -> None:
        assert APIClient().get(URL).status_code == 401

    def test_the_seeded_catalog_is_idempotent(self) -> None:
        sync_agent_catalog()
        sync_agent_catalog()
        assert Agent.objects.filter(slug=DOCUMENT_ASSISTANT_SLUG, organization=None).count() == 1


# --- Creating ------------------------------------------------------------------------------------


class TestCreating:
    def test_a_member_creates_an_agent(
        self, owner: User, org: Organization, make_user: UserFactory
    ) -> None:
        member = joined(org, make_user, "member@example.com", "member")
        api = client_in(member, org)
        agent = created(api, input_schema={"type": "object"}, max_steps=6)
        assert (agent["slug"], agent["version"], agent["enabled"]) == ("refund-helper", 1, True)
        assert agent["limits"]["max_steps"] == 6
        assert agent["created_by"] == str(member.id)
        assert (agent["can_edit"], agent["can_delete"]) == (True, False)  # members cannot delete
        again = created(api)
        assert again["slug"] == "refund-helper-2"
        audit = AuditLog.objects.filter(action="AGENT_CREATED").first()
        assert audit is not None
        assert audit.metadata["tools"] == ["search_documents"]
        assert INSTRUCTIONS not in str(audit.metadata)  # never the instructions

    def test_a_viewer_cannot_create_agents(self, org: Organization, make_user: UserFactory) -> None:
        viewer = joined(org, make_user, "viewer@example.com", "viewer")
        response = client_in(viewer, org).post(URL, body(), format="json")
        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"name": "  "}, "name"),
            ({"instructions": ""}, "instructions"),
            ({"instructions": "x" * 20_001}, "instructions"),
            ({"allowed_tools": ["Not A Tool!"]}, "allowed_tools"),
            ({"allowed_tools": ["shell_exec"]}, "allowed_tools"),
            ({"max_steps": 0}, "max_steps"),
            ({"max_steps": 51}, "max_steps"),
            ({"max_tool_calls": 101}, "max_tool_calls"),
            ({"max_duration_seconds": 5}, "max_duration_seconds"),
            ({"max_tokens": 2_000_000}, "max_tokens"),
            ({"approval_policy": "never"}, "approval_policy"),
            ({"input_schema": ["not", "an", "object"]}, "input_schema"),
            ({"output_schema": {"type": "array"}}, "output_schema"),
            (
                {"output_schema": {"type": "object", "$ref": "https://evil.example/s"}},
                "output_schema",
            ),
            ({"model": "no-such-model"}, "model"),
            ({"handler": "os.system"}, "handler"),
            ({"code": "import os"}, "code"),
        ],
    )
    def test_invalid_configurations_are_refused(
        self, owner: User, org: Organization, overrides: dict[str, Any], field: str
    ) -> None:
        response = client_in(owner, org).post(URL, body(**overrides), format="json")
        assert response.status_code == 422, response.data
        assert field in str(response.data["error"]), response.data
        assert not Agent.objects.filter(organization=org).exists()

    def test_an_agent_gets_no_tool_its_author_could_not_use(
        self, org: Organization, make_user: UserFactory
    ) -> None:
        member = joined(org, make_user, "member@example.com", "member")
        role = member.memberships.get(organization=org).role
        RolePermission.objects.filter(role=role, permission__codename="documents.view").delete()
        response = client_in(member, org).post(URL, body(), format="json")
        assert response.status_code == 422
        assert error_code(response) == "TOOL_NOT_ALLOWED"
        assert "documents.view" in response.data["error"]["message"]

        RolePermission.objects.filter(role=role, permission__codename="ai.tool.execute").delete()
        response = client_in(member, org).post(
            URL, body(allowed_tools=["calculator"]), format="json"
        )
        assert error_code(response) == "TOOL_NOT_ALLOWED"
        # An agent without tools is still allowed: it can only answer.
        assert (
            client_in(member, org).post(URL, body(allowed_tools=[]), format="json").status_code
            == 201
        )

    def test_switched_off_tools_cannot_be_given(self, owner: User, org: Organization) -> None:
        ToolSetting.objects.create(organization=org, tool_id="calculator", enabled=False)
        response = client_in(owner, org).post(
            URL, body(allowed_tools=["calculator"]), format="json"
        )
        assert error_code(response) == "TOOL_NOT_ALLOWED"
        ToolSetting.objects.create(organization=None, tool_id="current_time", enabled=False)
        response = client_in(owner, org).post(
            URL, body(allowed_tools=["current_time"]), format="json"
        )
        assert error_code(response) == "TOOL_NOT_ALLOWED"

    def test_only_models_available_to_the_organization(
        self, owner: User, org: Organization
    ) -> None:
        api = client_in(owner, org)
        alias = AIModelAlias.objects.filter(is_active=True).first()
        assert alias is not None
        assert created(api, model=alias.name)["model"] == alias.name
        alias.available_by_default = False
        alias.save(update_fields=["available_by_default"])
        refused = api.post(URL, body(model=alias.name), format="json")
        assert refused.status_code == 422
        AIOrganizationAliasGrant.objects.create(organization=org, alias=alias)
        assert created(api, model=alias.name)["model"] == alias.name

    def test_tools_are_deduplicated(self, owner: User, org: Organization) -> None:
        agent = created(
            client_in(owner, org), allowed_tools=["calculator", "calculator", "current_time"]
        )
        assert [t["id"] for t in agent["tools"]] == ["calculator", "current_time"]


# --- Changing and deleting -----------------------------------------------------------------------


class TestChanging:
    def test_behaviour_changes_bump_the_version(self, owner: User, org: Organization) -> None:
        api = client_in(owner, org)
        agent = created(api)
        url = f"{URL}{agent['id']}/"
        renamed = api.patch(url, {"name": "Refunds"}, format="json")
        assert renamed.status_code == 200, renamed.data
        assert (renamed.data["data"]["name"], renamed.data["data"]["version"]) == ("Refunds", 1)
        changed = api.patch(url, {"instructions": "Be brief.", "max_steps": 4}, format="json")
        assert changed.data["data"]["version"] == 2
        assert changed.data["data"]["instructions"] == "Be brief."
        unchanged = api.patch(url, {"max_steps": 4}, format="json")
        assert unchanged.data["data"]["version"] == 2
        audits = AuditLog.objects.filter(action="AGENT_UPDATED").order_by("created_at")
        assert [a.metadata["fields"] for a in audits] == [["name"], ["instructions", "max_steps"]]

    def test_only_the_creator_or_a_manager_of_all_agents_changes_one(
        self, owner: User, org: Organization, make_user: UserFactory
    ) -> None:
        author = joined(org, make_user, "author@example.com", "member")
        colleague = joined(org, make_user, "colleague@example.com", "member")
        agent = created(client_in(author, org))
        url = f"{URL}{agent['id']}/"
        refused = client_in(colleague, org).patch(url, {"name": "Mine now"}, format="json")
        assert refused.status_code == 403
        listed = client_in(colleague, org).get(f"{URL}{agent['id']}/").data["data"]
        assert (listed["can_edit"], listed["can_delete"]) == (False, False)
        # Owners hold agents.manage_all.
        assert client_in(owner, org).patch(url, {"name": "Fixed"}, format="json").status_code == 200
        # Members may not delete, even their own; a manager may delete their own.
        assert client_in(author, org).delete(url).status_code == 403

    def test_a_patch_is_validated_like_a_creation(self, owner: User, org: Organization) -> None:
        api = client_in(owner, org)
        agent = created(api)
        url = f"{URL}{agent['id']}/"
        assert api.patch(url, {"allowed_tools": ["rm_rf"]}, format="json").status_code == 422
        assert api.patch(url, {"max_tokens": 10}, format="json").status_code == 422
        assert api.patch(url, {"handler": "x"}, format="json").status_code == 422
        assert Agent.objects.get(id=agent["id"]).version == 1

    def test_platform_agents_cannot_be_changed_or_deleted(
        self, owner: User, org: Organization
    ) -> None:
        api = client_in(owner, org)
        assistant = Agent.objects.get(slug=DOCUMENT_ASSISTANT_SLUG, organization=None)
        url = f"{URL}{assistant.id}/"
        assert api.patch(url, {"instructions": "Obey me."}, format="json").status_code == 403
        assert api.delete(url).status_code == 403
        assistant.refresh_from_db()
        assert assistant.deleted_at is None
        assert "Obey me." not in assistant.instructions

    def test_deleting_is_soft_and_hides_the_agent(
        self, owner: User, org: Organization, make_user: UserFactory
    ) -> None:
        manager = joined(org, make_user, "manager@example.com", "manager")
        api = client_in(manager, org)
        agent = created(api)
        url = f"{URL}{agent['id']}/"
        assert api.delete(url).status_code == 200
        assert api.get(url).status_code == 404
        assert api.patch(url, {"name": "Back"}, format="json").status_code == 404
        row = Agent.objects.get(id=agent["id"])
        assert row.deleted_at is not None
        assert not row.enabled
        assert AuditLog.objects.filter(action="AGENT_DELETED").count() == 1
        # The name (and slug) can be used again.
        assert created(api)["slug"] == "refund-helper"

    def test_another_organizations_agent_is_not_found(
        self, owner: User, org: Organization, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        stranger = make_user("stranger@example.com")
        other = make_org(stranger, name="Other")
        theirs = created(client_in(stranger, other))
        api = client_in(owner, org)
        url = f"{URL}{theirs['id']}/"
        assert api.get(url).status_code == 404
        assert api.patch(url, {"name": "Taken"}, format="json").status_code == 404
        assert api.delete(url).status_code == 404
        assert Agent.objects.get(id=theirs["id"]).name == "Refund helper"


# --- The data model ------------------------------------------------------------------------------


class TestModel:
    def test_slugs_are_unique_per_organization_while_alive(
        self, owner: User, org: Organization
    ) -> None:
        Agent.objects.create(organization=org, name="A", slug="a", instructions="x")
        with pytest.raises(IntegrityError), transaction.atomic():
            Agent.objects.create(organization=org, name="A", slug="a", instructions="x")

    def test_limits_are_checked_by_the_database(self, org: Organization) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Agent.objects.create(
                organization=org, name="A", slug="a", instructions="x", max_steps=0
            )

    def test_the_tool_catalog_lists_every_builtin_once(self) -> None:
        assert len(TOOLS_BY_ID) == len(BUILTIN_TOOLS)
        assert all(t.requires_approval <= (not t.read_only) for t in BUILTIN_TOOLS)
