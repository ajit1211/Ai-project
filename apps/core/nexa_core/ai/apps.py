from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AIConfig(AppConfig):
    name = "nexa_core.ai"
    label = "ai"
    verbose_name = "AI"

    def ready(self) -> None:
        from nexa_core.ai.catalog import sync_after_migrate

        post_migrate.connect(sync_after_migrate, sender=self, dispatch_uid="sync_ai_catalog")
