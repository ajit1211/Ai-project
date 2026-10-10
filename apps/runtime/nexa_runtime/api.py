"""The `/api/v1` router for Runtime.

Runtime owns `/ai` (Phase 04, except `/ai/usage` which Core serves), `/conversations`
(Phase 05), and from later phases `/runs`, `/approvals`, `/search` and `/realtime` (doc 18.2).
"""

from fastapi import APIRouter

from nexa_runtime.ai.router import router as ai_router
from nexa_runtime.auth.router import router as auth_router
from nexa_runtime.chat.router import router as chat_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(ai_router)
api_router.include_router(chat_router)
