"""The AI catalog, policies and usage ledger on the Core side."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import Client

from nexa_core.ai.catalog import sync_ai_catalog
from nexa_core.ai.models import (
    AIAliasTarget,
    AIModel,
    AIModelAlias,
    AIModelPrice,
    AIUsageEvent,
)
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import Organization, OrganizationMembership
from nexa_core.testing import OrgFactory, UserFactory, add_member

pytestmark = pytest.mark.django_db


def _event(**overrides: object) -> AIUsageEvent:
    values: dict[str, object] = {
        "organization_id": uuid4(),
        "user_id": uuid4(),
        "feature": "playground",
        "alias": "nexa-balanced",
        "provider": "mock",
        "provider_model": "mock-1",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cost_micros": 0,
        "status": "success",
    }
    values.update(overrides)
    return AIUsageEvent.objects.create(**values)


class TestCatalog:
    def test_seed_is_idempotent_and_keeps_operator_edits(self) -> None:
        sync_ai_catalog()
        models, prices = AIModel.objects.count(), AIModelPrice.objects.count()
        alias = AIModelAlias.objects.get(name="nexa-fast")
        alias.is_active = False
        alias.save()
        AIAliasTarget.objects.filter(alias=alias, priority=2).delete()

        sync_ai_catalog()
        assert (AIModel.objects.count(), AIModelPrice.objects.count()) == (models, prices)
        alias.refresh_from_db()
        assert alias.is_active is False
        assert alias.targets.count() == 2

    def test_one_default_alias(self) -> None:
        assert AIModelAlias.objects.filter(is_default=True).count() == 1
        another = AIModelAlias.objects.get(name="nexa-fast")
        another.is_default = True
        with pytest.raises(DatabaseError), transaction.atomic():
            another.save()

    def test_price_in_force(self) -> None:
        model = AIModel.objects.get(provider_model="claude-sonnet-5-5")
        old = model.current_price()
        assert old is not None
        assert old.input_micros_per_million == 2_000_000
        AIModelPrice.objects.create(
            model=model,
            input_micros_per_million=1_500_000,
            output_micros_per_million=7_500_000,
            effective_from=datetime.now(UTC) - timedelta(minutes=1),
        )
        AIModelPrice.objects.create(
            model=model,
            input_micros_per_million=999,
            output_micros_per_million=999,
            effective_from=datetime.now(UTC) + timedelta(days=1),  # scheduled, not yet in force
        )
        current = model.current_price()
        assert current is not None
        assert current.input_micros_per_million == 1_500_000

    def test_models_cannot_be_deleted_while_priced_or_targeted(self) -> None:
        model = AIModel.objects.get(provider_model="mock-1")
        with pytest.raises(DatabaseError), transaction.atomic():
            model.delete()


class TestUsageLedger:
    def test_append_only_in_python_and_in_the_database(self) -> None:
        event = _event()
        event.status = "error"
        with pytest.raises(RuntimeError):
            event.save()
        with pytest.raises(RuntimeError):
            event.delete()
        # QuerySet.update bypasses the model; the trigger still refuses it, for every role.
        with pytest.raises(DatabaseError), transaction.atomic():
            AIUsageEvent.objects.filter(id=event.id).update(status="error")
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM ai_usage_events WHERE id = %s", [event.id])
        assert AIUsageEvent.objects.get(id=event.id).status == "success"

    def test_total_tokens_is_checked(self) -> None:
        with pytest.raises(DatabaseError), transaction.atomic():
            _event(input_tokens=1, output_tokens=1, total_tokens=5)


class TestUsageEndpoint:
    @pytest.fixture
    def viewer_and_owner(
        self, make_user: UserFactory, make_org: OrgFactory
    ) -> tuple[User, User, Organization]:
        owner = make_user()
        org = make_org(owner)
        viewer = make_user()
        add_member(org, viewer, "viewer")
        return owner, viewer, org

    def _headers(self, user: User) -> dict[str, str]:
        issued = start_session(user, client_type="web")
        return {"Authorization": f"Bearer {issued.access_token}"}

    def test_scopes_and_permissions(
        self, client: Client, viewer_and_owner: tuple[User, User, Organization]
    ) -> None:
        owner, viewer, org = viewer_and_owner
        for user in (owner, viewer):
            _event(organization_id=org.id, user_id=user.id)
        _event(organization_id=uuid4(), user_id=viewer.id)  # another organization's row

        mine = client.get("/api/v1/ai/usage/?scope=me", headers=self._headers(viewer))
        assert mine.status_code == 200, mine.content
        assert mine.json()["data"]["totals"]["requests"] == 1  # only this organization

        whole = client.get("/api/v1/ai/usage/?scope=organization", headers=self._headers(viewer))
        assert whole.status_code == 403  # viewers lack analytics.view

        whole = client.get("/api/v1/ai/usage/?scope=organization", headers=self._headers(owner))
        assert whole.status_code == 200
        assert whole.json()["data"]["totals"]["requests"] == 2

        bad = client.get("/api/v1/ai/usage/?scope=everyone", headers=self._headers(owner))
        assert bad.status_code == 422
        bad = client.get("/api/v1/ai/usage/?days=400", headers=self._headers(owner))
        assert bad.status_code == 422

    def test_requires_a_token(self, client: Client) -> None:
        assert client.get("/api/v1/ai/usage/").status_code == 401

    def test_requires_an_active_membership(
        self, client: Client, viewer_and_owner: tuple[User, User, Organization]
    ) -> None:
        _owner, viewer, org = viewer_and_owner
        headers = self._headers(viewer)
        OrganizationMembership.objects.filter(user=viewer, organization=org).update(
            status="suspended"
        )
        assert client.get("/api/v1/ai/usage/", headers=headers).status_code == 404
