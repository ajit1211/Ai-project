"""The tool tables on the Core side (Phase 10): tenant consistency, cascades in the database
(Runtime deletes messages with plain SQL), one setting per tool and scope, status rules, the
admin (audited switches, no private arguments shown), the new permission, and the mock model's
tool calling."""

from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.utils import timezone

from nexa_core.access.catalog import ROLE_TEMPLATES_BY_KEY
from nexa_core.ai.models import AIModel
from nexa_core.audit.models import AuditLog
from nexa_core.chat.models import Conversation, Message
from nexa_core.testing import OrgFactory, UserFactory
from nexa_core.tools.models import ToolApproval, ToolInvocation, ToolSetting

pytestmark = pytest.mark.django_db


@pytest.fixture
def reply(make_user: UserFactory, make_org: OrgFactory) -> Message:
    owner = make_user()
    organization = make_org(owner)
    conversation = Conversation.objects.create(organization=organization, owner=owner)
    Message.objects.create(
        organization=organization, conversation=conversation, sequence=1, role="user", content="q"
    )
    return Message.objects.create(
        organization=organization,
        conversation=conversation,
        sequence=2,
        role="assistant",
        status="awaiting_approval",
    )


def invocation(reply: Message, **extra: Any) -> ToolInvocation:
    values: dict[str, Any] = {
        "organization": reply.organization,
        "conversation": reply.conversation,
        "message": reply,
        "user": reply.conversation.owner,
        "step": 0,
        "position": 0,
        "call_id": "call_1",
        "tool_id": "save_memory",
        "tool_version": "1",
        "status": "awaiting_approval",
        "risk": "high",
        "arguments": {"content": "x"},
        "arguments_hash": "a" * 64,
        "idempotency_key": f"key-{extra.get('position', 0)}-{extra.get('call_id', 'call_1')}",
    }
    values.update(extra)
    return ToolInvocation.objects.create(**values)


def approval(call: ToolInvocation, **extra: Any) -> ToolApproval:
    values: dict[str, Any] = {
        "organization": call.organization,
        "invocation": call,
        "requested_by": call.user,
        "tool_id": call.tool_id,
        "summary": "Saving to your memories",
        "arguments_hash": call.arguments_hash,
        "risk": "high",
        "expires_at": timezone.now() + timedelta(minutes=15),
    }
    values.update(extra)
    return ToolApproval.objects.create(**values)


def test_an_invocation_stays_in_its_messages_organization(
    reply: Message, make_user: UserFactory, make_org: OrgFactory
) -> None:
    other = make_org(make_user("other@example.com"), name="Other")
    with pytest.raises(IntegrityError), transaction.atomic():
        invocation(reply, organization=other)


def test_an_approval_stays_in_its_invocations_organization(
    reply: Message, make_user: UserFactory, make_org: OrgFactory
) -> None:
    call = invocation(reply)
    other = make_org(make_user("other@example.com"), name="Other")
    with pytest.raises(IntegrityError), transaction.atomic():
        approval(call, organization=other)


def test_a_call_is_recorded_once(reply: Message) -> None:
    invocation(reply)
    with pytest.raises(IntegrityError), transaction.atomic():
        invocation(reply, position=1, idempotency_key="key-0-call_1")  # same key
    with pytest.raises(IntegrityError), transaction.atomic():
        invocation(reply, idempotency_key="another")  # same step and position


def test_only_known_statuses(reply: Message) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        invocation(reply, status="done")
    call = invocation(reply)
    with pytest.raises(IntegrityError), transaction.atomic():
        approval(call, status="maybe")


def test_deleting_a_reply_in_sql_deletes_its_tool_records(reply: Message) -> None:
    approval(invocation(reply))
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM messages WHERE id = %s", [reply.id])
    assert not ToolInvocation.objects.exists()
    assert not ToolApproval.objects.exists()


def test_one_setting_per_tool_and_scope(reply: Message) -> None:
    organization = reply.organization
    ToolSetting.objects.create(organization=None, tool_id="calculator", enabled=False)
    ToolSetting.objects.create(organization=organization, tool_id="calculator", enabled=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        ToolSetting.objects.create(organization=None, tool_id="calculator", enabled=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        ToolSetting.objects.create(organization=organization, tool_id="calculator", enabled=False)


def test_only_administrators_manage_tools() -> None:
    for key in ("owner", "admin"):
        assert "ai.tool.manage" in ROLE_TEMPLATES_BY_KEY[key].permissions, key
    for key in ("manager", "member", "viewer"):
        assert "ai.tool.manage" not in ROLE_TEMPLATES_BY_KEY[key].permissions, key
    assert "ai.tool.execute" in ROLE_TEMPLATES_BY_KEY["member"].permissions
    assert "ai.tool.execute" not in ROLE_TEMPLATES_BY_KEY["viewer"].permissions


def test_the_mock_model_can_call_tools() -> None:
    mock = AIModel.objects.get(provider="mock", provider_model="mock-1")
    assert "tool_calling" in mock.capabilities


def test_the_admin_audits_switches_and_hides_arguments(
    client: Client, reply: Message, make_user: UserFactory
) -> None:
    staff = make_user("staff@example.com", is_staff=True, is_superuser=True)
    client.force_login(staff)
    response = client.post(
        "/admin/tools/toolsetting/add/", {"tool_id": "calculator", "enabled": ""}
    )
    assert response.status_code == 302, response.content[:500]
    setting = ToolSetting.objects.get()
    assert (setting.enabled, setting.updated_by_id) == (False, staff.id)
    entry = AuditLog.objects.get(action="TOOL_SETTINGS_CHANGED")
    assert entry.metadata == {"enabled": False, "scope": "platform"}

    call = invocation(reply, arguments={"content": "my private note"})
    page = client.get(f"/admin/tools/toolinvocation/{call.id}/change/")
    assert page.status_code == 200
    assert b"my private note" not in page.content
