"""The shared authorization policy: tenant first, then role ceiling, then resource access."""

import uuid

import pytest

from nexa_common.access import Access, Principal, ResourceRef, authorize, can, require
from nexa_common.errors import NotFound, PermissionDenied

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
TEAM = uuid.uuid4()


def principal(
    *permissions: str, org: uuid.UUID = ORG_A, teams: frozenset[uuid.UUID] = frozenset()
) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        organization_id=org,
        permissions=frozenset(permissions),
        team_ids=teams,
    )


def knowledge_base(**overrides: object) -> ResourceRef:
    fields: dict[str, object] = {"type": "knowledge", "id": uuid.uuid4(), "organization_id": ORG_A}
    fields.update(overrides)
    return ResourceRef(**fields)  # type: ignore[arg-type]


def test_other_tenants_resources_are_invisible_even_with_every_permission() -> None:
    admin_of_b = principal("knowledge.view", "knowledge.manage_all", org=ORG_B)
    with pytest.raises(NotFound):
        authorize(admin_of_b, "view", knowledge_base())


def test_role_is_a_ceiling_that_ownership_cannot_raise() -> None:
    owner_without_permission = principal("knowledge.view")
    resource = knowledge_base(owner_id=owner_without_permission.user_id)
    assert can(owner_without_permission, "view", resource)
    assert not can(owner_without_permission, "delete", resource)


def test_owner_can_manage_their_own_resource() -> None:
    member = principal("knowledge.view", "knowledge.update", "knowledge.delete")
    assert can(member, "delete", knowledge_base(owner_id=member.user_id))


def test_permission_alone_does_not_grant_access_to_someone_elses_private_resource() -> None:
    member = principal("knowledge.view", "knowledge.update")
    private = knowledge_base(owner_id=uuid.uuid4(), visibility="private")
    with pytest.raises(NotFound):
        authorize(member, "view", private)


def test_team_visibility_admits_team_members_only() -> None:
    resource = knowledge_base(owner_id=uuid.uuid4(), visibility="team", team_id=TEAM)
    assert can(principal("knowledge.view", teams=frozenset({TEAM})), "view", resource)
    assert not can(principal("knowledge.view"), "view", resource)


def test_explicit_grant_raises_access_within_the_role_ceiling() -> None:
    editor = principal("knowledge.view", "knowledge.update")
    resource = knowledge_base(owner_id=uuid.uuid4(), grants={editor.user_id: Access.EDIT})
    assert can(editor, "update", resource)
    assert not can(editor, "delete", resource)


def test_visible_but_not_editable_is_forbidden_rather_than_hidden() -> None:
    member = principal("knowledge.view", "knowledge.update")
    with pytest.raises(PermissionDenied):
        authorize(member, "update", knowledge_base(owner_id=uuid.uuid4()))


def test_manage_all_lets_administrators_act_on_any_resource_in_their_org() -> None:
    admin = principal("knowledge.view", "knowledge.delete", "knowledge.manage_all")
    assert can(admin, "delete", knowledge_base(owner_id=uuid.uuid4(), visibility="private"))


def test_agent_can_never_exceed_the_user_it_acts_for() -> None:
    user = principal("ai.chat", "knowledge.view")
    agent = user.for_agent(uuid.uuid4(), ["ai.chat", "ai.database_query", "knowledge.view"])
    assert agent.actor_type == "agent"
    assert agent.permissions == {"ai.chat", "knowledge.view"}
    with pytest.raises(PermissionDenied):
        require(agent, "ai.database_query")
