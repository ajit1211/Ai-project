"""`GET /api/v1/runtime/whoami`: what Runtime knows about the caller, from verified sources only."""

from fastapi import APIRouter
from pydantic import BaseModel

from nexa_runtime.auth.dependencies import CurrentPrincipal, CurrentUser
from nexa_runtime.responses import Envelope, ok

router = APIRouter(prefix="/runtime", tags=["runtime"])


class WhoAmI(BaseModel):
    user_id: str
    email: str
    session_id: str
    organization_id: str
    role: str | None
    permissions: list[str]


@router.get("/whoami")
async def whoami(user: CurrentUser, principal: CurrentPrincipal) -> Envelope[WhoAmI]:
    return ok(
        WhoAmI(
            user_id=str(user.user_id),
            email=user.email,
            session_id=str(user.session_id),
            organization_id=str(principal.organization_id),
            role=principal.role_key,
            permissions=sorted(principal.permissions),
        )
    )
