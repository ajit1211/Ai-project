"""The chat schema on the Core side: tenant consistency, ordering and idempotency constraints,
and the conversation permissions every role carries."""

import pytest
from django.db import IntegrityError, transaction

from nexa_core.access.catalog import ROLE_TEMPLATES
from nexa_core.chat.models import Conversation, Message
from nexa_core.testing import OrgFactory, UserFactory

pytestmark = pytest.mark.django_db


def _message(conversation: Conversation, sequence: int, **extra: object) -> Message:
    values: dict[str, object] = {
        "organization": conversation.organization,
        "conversation": conversation,
        "sequence": sequence,
        "role": "user",
        "content": "hi",
    }
    values.update(extra)
    return Message.objects.create(**values)


@pytest.fixture
def conversation(make_user: UserFactory, make_org: OrgFactory) -> Conversation:
    owner = make_user()
    return Conversation.objects.create(organization=make_org(owner), owner=owner)


def test_a_message_cannot_point_at_another_tenants_conversation(
    conversation: Conversation, make_user: UserFactory, make_org: OrgFactory
) -> None:
    other = make_org(make_user(), name="Other")
    with pytest.raises(IntegrityError), transaction.atomic():
        _message(conversation, 1, organization=other)


def test_sequence_is_unique_per_conversation(conversation: Conversation) -> None:
    _message(conversation, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        _message(conversation, 1)


def test_client_message_id_is_unique_per_conversation_when_set(conversation: Conversation) -> None:
    _message(conversation, 1, client_message_id="abc")
    _message(conversation, 2)  # empty ids do not collide
    _message(conversation, 3)
    with pytest.raises(IntegrityError), transaction.atomic():
        _message(conversation, 4, client_message_id="abc")


def test_deleting_a_conversation_deletes_its_messages(conversation: Conversation) -> None:
    _message(conversation, 1)
    conversation.delete()
    assert Message.objects.count() == 0


def test_every_role_can_use_its_own_conversations() -> None:
    needed = {
        "conversation.view",
        "conversation.create",
        "conversation.update",
        "conversation.delete",
    }
    for template in ROLE_TEMPLATES:
        assert needed <= template.permissions, template.key
