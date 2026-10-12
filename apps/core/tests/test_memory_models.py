"""The memory schema on the Core side: scope targets, one active memory per subject, tenant
consistency through composite keys, what deletions leave behind, the search column, and the
memory permissions each role carries."""

import hashlib
from typing import Any

import pytest
from django.contrib.postgres.search import SearchQuery
from django.db import IntegrityError, transaction

from nexa_core.access.catalog import ROLE_TEMPLATES
from nexa_core.chat.models import Conversation, Message
from nexa_core.identity.models import User
from nexa_core.memory.models import ConversationSummary, Memory, MemoryExtraction
from nexa_core.organizations.models import Organization, Team
from nexa_core.testing import OrgFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(make_user: UserFactory) -> User:
    return make_user()


@pytest.fixture
def org(owner: User, make_org: OrgFactory) -> Organization:
    return make_org(owner)


@pytest.fixture
def conversation(org: Organization, owner: User) -> Conversation:
    return Conversation.objects.create(organization=org, owner=owner)


def memory(org: Organization, owner: User, content: str = "Prefers Python", **extra: Any) -> Memory:
    values: dict[str, Any] = {
        "organization": org,
        "owner": owner,
        "scope": Memory.Scope.USER,
        "kind": Memory.Kind.PREFERENCE,
        "key": "",
        "content": content,
        "source_type": Memory.SourceType.MANUAL,
        "status": Memory.Status.ACTIVE,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }
    values.update(extra)
    return Memory.objects.create(**values)


def rejected(**values: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        memory(**values)


class TestConstraints:
    def test_a_team_exactly_when_the_scope_is_the_team(
        self, org: Organization, owner: User
    ) -> None:
        team = Team.objects.create(organization=org, name="Platform", slug="platform")
        rejected(org=org, owner=owner, scope=Memory.Scope.TEAM)  # no team
        rejected(org=org, owner=owner, scope=Memory.Scope.USER, team=team)
        assert memory(org, owner, scope=Memory.Scope.TEAM, team=team).team == team

    def test_one_active_memory_per_subject_and_scope_target(
        self, org: Organization, owner: User, make_user: UserFactory, conversation: Conversation
    ) -> None:
        first = memory(org, owner, "Python", key="preferred_language")
        rejected(org=org, owner=owner, content="Go", key="preferred_language")
        # Once superseded, the subject is free again.
        first.status = Memory.Status.SUPERSEDED
        first.save(update_fields=["status"])
        memory(org, owner, "Go", key="preferred_language")
        # Keys are per owner, per scope target, and empty keys never collide.
        memory(org, make_user(), "Rust", key="preferred_language")
        memory(
            org,
            owner,
            "Here: Rust",
            key="preferred_language",
            scope="conversation",
            conversation=conversation,
        )
        memory(org, owner, "a", key="")
        memory(org, owner, "b", key="")

    def test_confidence_and_importance_ranges(self, org: Organization, owner: User) -> None:
        rejected(org=org, owner=owner, confidence=1.5)
        rejected(org=org, owner=owner, importance=0)
        rejected(org=org, owner=owner, importance=6)

    def test_references_stay_inside_the_organization(
        self, org: Organization, owner: User, make_user: UserFactory, make_org: OrgFactory
    ) -> None:
        stranger = make_user()
        other = make_org(stranger, name="Other")
        foreign_conversation = Conversation.objects.create(organization=other, owner=stranger)
        foreign_team = Team.objects.create(organization=other, name="Theirs", slug="theirs")
        rejected(org=org, owner=owner, conversation=foreign_conversation)
        rejected(org=org, owner=owner, scope=Memory.Scope.TEAM, team=foreign_team)
        with pytest.raises(IntegrityError), transaction.atomic():
            ConversationSummary.objects.create(
                organization=org,
                conversation=foreign_conversation,
                version=1,
                through_sequence=1,
                content="x",
            )

    def test_summary_versions_are_unique_per_conversation(
        self, org: Organization, conversation: Conversation
    ) -> None:
        ConversationSummary.objects.create(
            organization=org, conversation=conversation, version=1, through_sequence=4, content="a"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ConversationSummary.objects.create(
                organization=org,
                conversation=conversation,
                version=1,
                through_sequence=6,
                content="b",
            )

    def test_one_extraction_per_message(
        self, org: Organization, conversation: Conversation
    ) -> None:
        message = Message.objects.create(
            organization=org, conversation=conversation, sequence=1, role="user", content="hi"
        )
        MemoryExtraction.objects.create(
            organization=org, conversation=conversation, message=message
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            MemoryExtraction.objects.create(
                organization=org, conversation=conversation, message=message
            )


class TestDeletion:
    def test_deleting_a_conversation_keeps_memories_but_forgets_the_source(
        self, org: Organization, owner: User, conversation: Conversation
    ) -> None:
        message = Message.objects.create(
            organization=org, conversation=conversation, sequence=1, role="user", content="hi"
        )
        kept = memory(org, owner, conversation=conversation, source_message=message)
        ConversationSummary.objects.create(
            organization=org, conversation=conversation, version=1, through_sequence=1, content="s"
        )
        MemoryExtraction.objects.create(
            organization=org, conversation=conversation, message=message
        )
        conversation.delete()
        kept.refresh_from_db()
        assert kept.content == "Prefers Python"
        assert kept.conversation is None
        assert kept.source_message is None
        assert not ConversationSummary.objects.exists()
        assert not MemoryExtraction.objects.exists()

    def test_deleting_a_team_deletes_its_memories(self, org: Organization, owner: User) -> None:
        team = Team.objects.create(organization=org, name="Platform", slug="platform")
        memory(org, owner, "Use FastAPI", scope=Memory.Scope.TEAM, team=team)
        team.delete()
        assert not Memory.objects.exists()

    def test_a_superseded_memory_outlives_its_successor(
        self, org: Organization, owner: User
    ) -> None:
        old = memory(org, owner, "Django", key="framework", status=Memory.Status.SUPERSEDED)
        new = memory(org, owner, "FastAPI", key="framework")
        old.superseded_by = new
        old.save(update_fields=["superseded_by"])
        new.delete()
        old.refresh_from_db()
        assert old.superseded_by is None


def test_content_is_searchable(org: Organization, owner: User) -> None:
    memory(org, owner, "Prefers Django for web projects")
    memory(org, owner, "Lives in Pune")
    found = Memory.objects.filter(search=SearchQuery("django", config="english"))
    assert [m.content for m in found] == ["Prefers Django for web projects"]


def test_every_role_can_use_memories_and_only_managers_manage_shared_ones() -> None:
    own = {"memory.view", "memory.create", "memory.update", "memory.delete"}
    for template in ROLE_TEMPLATES:
        assert own <= template.permissions, template.key
        assert ("memory.manage_all" in template.permissions) == (
            template.key in {"owner", "admin", "manager"}
        ), template.key
