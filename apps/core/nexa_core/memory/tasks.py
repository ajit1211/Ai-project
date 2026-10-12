"""Retention: what happens to memories nobody uses any more (docs/architecture/memory-engine.md#retention).

Runs from a worker on a schedule (`memory.purge` below; the schedule is operations
configuration, see memory-engine.md). Everything here is also enforced at read time by
Runtime, which never recalls an expired memory, so the purge only tidies up:

1. Active memories and candidates past `expires_at` become `expired`.
2. A member's memories unused for longer than their `retention_days` become `expired`.
3. Conversation-scoped memories whose conversation is gone are deleted.
4. Expired and rejected memories older than `PURGE_AFTER` are deleted.
"""

from datetime import timedelta

from celery import shared_task
from django.db.models import F, Q
from django.utils import timezone

from nexa_core.memory.models import Memory, MemorySettings

PURGE_AFTER = timedelta(days=30)


def purge_expired_memories() -> dict[str, int]:
    now = timezone.now()
    live = (Memory.Status.ACTIVE, Memory.Status.CANDIDATE)
    expired = Memory.objects.filter(status__in=live, expires_at__lt=now).update(
        status=Memory.Status.EXPIRED, updated_at=now
    )
    retired = 0
    for settings in MemorySettings.objects.filter(retention_days__isnull=False):
        cutoff = now - timedelta(days=settings.retention_days or 0)
        retired += (
            Memory.objects.filter(
                organization=settings.organization,
                owner=settings.user,
                scope__in=Memory.PRIVATE_SCOPES,
                status__in=live,
            )
            .annotate(used_at=F("last_accessed_at"))
            .filter(Q(used_at__lt=cutoff) | Q(used_at__isnull=True, created_at__lt=cutoff))
            .update(status=Memory.Status.EXPIRED, updated_at=now)
        )
    orphaned, _ = Memory.objects.filter(
        scope=Memory.Scope.CONVERSATION, conversation__isnull=True
    ).delete()
    purged, _ = Memory.objects.filter(
        status__in=(Memory.Status.EXPIRED, Memory.Status.REJECTED),
        updated_at__lt=now - PURGE_AFTER,
    ).delete()
    return {"expired": expired, "retired": retired, "orphaned": orphaned, "purged": purged}


@shared_task(name="memory.purge")
def purge() -> dict[str, int]:
    return purge_expired_memories()
