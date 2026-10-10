"""The validated context every AI call carries. Built server-side, never from the request body."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from nexa_common.access import Principal
from nexa_common.errors import PermissionDenied


@dataclass(frozen=True, slots=True)
class AIRequestContext:
    request_id: str | None
    organization_id: UUID
    user_id: UUID
    permissions: frozenset[str]
    actor_type: str = "user"
    agent_id: UUID | None = None
    # What the call is for, recorded on the usage row: a message in Phase 05, an agent run in
    # Phase 11. Set by the calling service, never by the client.
    source_type: str | None = None
    source_id: UUID | None = None

    @classmethod
    def from_principal(cls, principal: Principal, *, request_id: str | None) -> "AIRequestContext":
        if principal.organization_id is None:
            raise PermissionDenied("Select an organization first.")
        return cls(
            request_id=request_id,
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            permissions=principal.permissions,
            actor_type=principal.actor_type,
            agent_id=principal.agent_id,
        )

    @property
    def end_user_id(self) -> str:
        """An opaque, stable identifier providers may use for abuse detection. Not reversible
        to a NEXA user ID without the database, and never an email address."""
        return hashlib.sha256(f"nexa:{self.user_id}".encode()).hexdigest()[:32]
