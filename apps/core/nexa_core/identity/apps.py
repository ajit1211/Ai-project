from django.apps import AppConfig
from django.db.models.signals import post_save


class IdentityConfig(AppConfig):
    name = "nexa_core.identity"
    label = "identity"
    verbose_name = "Identity"

    def ready(self) -> None:
        from nexa_core.identity.models import User
        from nexa_core.identity.signals import create_profile

        post_save.connect(create_profile, sender=User, dispatch_uid="identity_create_profile")
