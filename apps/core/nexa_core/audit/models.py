"""The append-only audit log (doc 20.7).

`organization_id` and `actor_id` are plain UUID columns rather than foreign keys, so records
survive the deletion of the organization or user they describe. Rows are never updated or
deleted by the application; retention is handled by a purge job (Phase 17) and monthly
partitioning (Phase 18).
"""

from typing import Any

from django.db import models

from nexa_common.ids import new_id
from nexa_core.audit.actions import Category, Severity


class AuditLog(models.Model):
    class ActorType(models.TextChoices):
        USER = "user"
        SYSTEM = "system"
        API_KEY = "api_key"
        AGENT = "agent"

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    organization_id = models.UUIDField(null=True, blank=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=64)
    category = models.CharField(
        max_length=16, choices=[(c.value, c.value) for c in Category], default=Category.AUDIT
    )
    severity = models.CharField(
        max_length=16, choices=[(s.value, s.value) for s in Severity], default=Severity.INFO
    )
    resource_type = models.CharField(max_length=40, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_logs"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["organization_id", "-created_at"], name="audit_logs_org_idx"),
            models.Index(fields=["actor_id", "-created_at"], name="audit_logs_actor_idx"),
            models.Index(fields=["category", "-created_at"], name="audit_logs_category_idx"),
            models.Index(fields=["action"], name="audit_logs_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action} at {self.created_at:%Y-%m-%d %H:%M:%S}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise RuntimeError("Audit log entries are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise RuntimeError("Audit log entries are append-only")
