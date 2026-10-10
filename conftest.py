"""Repository-wide test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _access_catalog(request: pytest.FixtureRequest) -> None:
    """Keep the permission catalog present for every database test.

    Migrations create the catalog once, but tests that commit (`transaction=True`) flush every
    table afterwards, catalog included. Restoring it here means test order never matters.
    """
    marker = request.node.get_closest_marker("django_db")
    if marker is None and not {"db", "transactional_db"} & set(request.fixturenames):
        return
    transactional = marker is not None and marker.kwargs.get("transaction", False)
    request.getfixturevalue("transactional_db" if transactional else "db")

    from nexa_core.ai.catalog import sync_ai_catalog
    from nexa_core.ai.models import AIModelAlias
    from nexa_core.organizations.models import Permission
    from nexa_core.organizations.roles import sync_access_catalog

    if not Permission.objects.exists():
        sync_access_catalog()
    if not AIModelAlias.objects.exists():
        sync_ai_catalog()
