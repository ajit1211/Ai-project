from django.apps import AppConfig
from django.db.models.signals import post_migrate


class OrganizationsConfig(AppConfig):
    name = "nexa_core.organizations"
    label = "organizations"
    verbose_name = "Organizations"

    def ready(self) -> None:
        from nexa_core.organizations.roles import sync_after_migrate

        post_migrate.connect(sync_after_migrate, sender=self, dispatch_uid="sync_access_catalog")
