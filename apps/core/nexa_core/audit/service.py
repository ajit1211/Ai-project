"""Record audit events. Call sites state what happened; context (IP, user agent, request ID)
is attached automatically and secrets are stripped from metadata."""

import uuid
from typing import Any

from nexa_common.context import current_request_id
from nexa_common.logs import get_logger
from nexa_core import request_meta
from nexa_core.audit.actions import AuditAction, Severity, spec_for
from nexa_core.audit.models import AuditLog

log = get_logger("nexa.core.audit")

_SECRET_MARKERS = ("password", "token", "secret", "authorization", "cookie", "key")


def record(
    action: AuditAction,
    *,
    actor_id: uuid.UUID | None = None,
    actor_type: str = AuditLog.ActorType.USER,
    organization_id: uuid.UUID | None = None,
    resource_type: str = "",
    resource_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    spec = spec_for(action)
    meta = request_meta.current()
    entry = AuditLog.objects.create(
        organization_id=organization_id,
        actor_type=actor_type if actor_id else AuditLog.ActorType.SYSTEM,
        actor_id=actor_id,
        action=action.value,
        category=spec.category.value,
        severity=spec.severity.value,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else "",
        ip_address=meta.ip_address,
        user_agent=meta.user_agent,
        request_id=current_request_id() or "",
        metadata=redact(metadata or {}),
    )
    if spec.severity is not Severity.INFO:
        # Security events also go to the log stream, where monitoring and alerting watch.
        log.warning(
            "security.event",
            action=action.value,
            severity=spec.severity.value,
            actor_id=str(actor_id) if actor_id else None,
            organization_id=str(organization_id) if organization_id else None,
        )
    return entry


def redact(value: Any) -> Any:
    """Drop anything that looks like a credential, at any depth."""
    if isinstance(value, dict):
        return {
            key: redact(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in _SECRET_MARKERS)
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
