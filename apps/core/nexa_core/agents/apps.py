from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AgentsConfig(AppConfig):
    name = "nexa_core.agents"
    label = "agents"
    verbose_name = "AI agents"

    def ready(self) -> None:
        from nexa_core.agents.catalog import sync_after_migrate

        post_migrate.connect(sync_after_migrate, sender=self, dispatch_uid="sync_agent_catalog")
