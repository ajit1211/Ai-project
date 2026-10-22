"""Tool and agent-run events in the shared audit log (`audit_logs`, owned by Core's audit app).

The action names, categories and severities are the ones Core declares
(nexa_core.audit.actions: TOOL_*, AGENT_RUN_*), so investigations see one timeline whichever
service wrote the row. Metadata carries ids, the tool, its version, the outcome and error codes: never
arguments, results, document text or anything secret. A failed audit write is logged and
never fails the call it describes.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_common.ids import new_id
from nexa_common.logs import get_logger
from nexa_runtime.tools import tables as t
from nexa_runtime.tools.repository import now

log = get_logger("nexa.runtime.tools.audit")

# action -> (category, severity), mirroring nexa_core.audit.actions.ACTION_SPECS
ACTIONS: dict[str, tuple[str, str]] = {
    "TOOL_EXECUTED": ("audit", "info"),
    "TOOL_EXECUTION_FAILED": ("audit", "info"),
    "TOOL_INVOCATION_REJECTED": ("security", "info"),
    "TOOL_PERMISSION_DENIED": ("security", "warning"),
    "TOOL_RATE_LIMITED": ("security", "warning"),
    "TOOL_APPROVAL_REQUESTED": ("audit", "info"),
    "TOOL_APPROVAL_DECIDED": ("audit", "info"),
    "TOOL_SETTINGS_CHANGED": ("security", "info"),
    "AGENT_RUN_STARTED": ("audit", "info"),
    "AGENT_RUN_COMPLETED": ("audit", "info"),
    "AGENT_RUN_FAILED": ("audit", "info"),
    "AGENT_RUN_CANCELLED": ("audit", "info"),
    "AGENT_RUN_PAUSED": ("audit", "info"),
    "AGENT_RUN_RESUMED": ("audit", "info"),
}


class ToolAudit:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        action: str,
        *,
        organization_id: UUID | None,
        actor_id: UUID | None,
        resource_type: str,
        resource_id: str,
        request_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        category, severity = ACTIONS[action]
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(t.audit_logs).values(
                        id=new_id(),
                        organization_id=organization_id,
                        actor_type="user" if actor_id else "system",
                        actor_id=actor_id,
                        action=action,
                        category=category,
                        severity=severity,
                        resource_type=resource_type,
                        resource_id=resource_id[:64],
                        ip_address=None,
                        user_agent="",
                        request_id=(request_id or "")[:128],
                        metadata=metadata,
                        created_at=now(),
                    )
                )
        except Exception as exc:
            log.error("tools.audit.write_failed", action=action, error=type(exc).__name__)
            return
        if severity != "info":
            log.warning("security.event", action=action, severity=severity)
