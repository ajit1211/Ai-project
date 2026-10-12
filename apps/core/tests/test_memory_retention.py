"""The retention purge: expiry, per-member retention, orphans, and the final delete."""

import hashlib
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from nexa_core.chat.models import Conversation
from nexa_core.identity.models import User
from nexa_core.memory.models import Memory, MemorySettings
from nexa_core.memory.tasks import purge_expired_memories
from nexa_core.organizations.models import Organization
from nexa_core.testing import OrgFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(make_user: UserFactory) -> User:
    return make_user()


@pytest.fixture
def org(owner: User, make_org: OrgFactory) -> Organization:
    return make_org(owner)


def memory(org: Organization, owner: User, content: str, **extra: Any) -> Memory:
    values: dict[str, Any] = {
        "organization": org,
        "owner": owner,
        "scope": Memory.Scope.USER,
        "kind": Memory.Kind.FACT,
        "content": content,
        "source_type": Memory.SourceType.MANUAL,
        "status": Memory.Status.ACTIVE,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }
    values.update(extra)
    return Memory.objects.create(**values)


def test_the_purge_expires_retires_and_deletes(org: Organization, owner: User) -> None:
    now = timezone.now()
    past_due = memory(org, owner, "past due", expires_at=now - timedelta(minutes=1))
    still_good = memory(org, owner, "still good", expires_at=now + timedelta(days=1))
    MemorySettings.objects.create(organization=org, user=owner, retention_days=30)
    unused = memory(org, owner, "unused", last_accessed_at=now - timedelta(days=31))
    never_used = memory(org, owner, "never used")
    Memory.objects.filter(id=never_used.id).update(created_at=now - timedelta(days=31))
    recent = memory(org, owner, "recent", last_accessed_at=now - timedelta(days=1))
    conversation = Conversation.objects.create(organization=org, owner=owner)
    orphan = memory(
        org, owner, "orphan", scope=Memory.Scope.CONVERSATION, conversation=conversation
    )
    conversation.delete()
    old_reject = memory(org, owner, "old reject", status=Memory.Status.REJECTED)
    Memory.objects.filter(id=old_reject.id).update(updated_at=now - timedelta(days=31))

    counts = purge_expired_memories()

    assert counts == {"expired": 1, "retired": 2, "orphaned": 1, "purged": 1}
    assert Memory.objects.get(id=past_due.id).status == "expired"
    assert Memory.objects.get(id=still_good.id).status == "active"
    assert Memory.objects.get(id=unused.id).status == "expired"
    assert Memory.objects.get(id=never_used.id).status == "expired"
    assert Memory.objects.get(id=recent.id).status == "active"
    assert not Memory.objects.filter(id__in=[orphan.id, old_reject.id]).exists()
    # Freshly expired rows stay for a while, so the user can see what was dropped.
    assert Memory.objects.filter(status="expired").count() == 3
