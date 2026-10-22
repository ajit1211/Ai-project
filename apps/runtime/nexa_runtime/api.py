"""The `/api/v1` router for Runtime.

Runtime owns `/ai` (Phase 04, except `/ai/usage` which Core serves), `/conversations`
(Phase 05), `/memories` (Phase 06), `/tools` (Phase 10), and from later phases `/runs`,
`/search` and `/realtime` (doc 18.2). Approvals of tool actions are conversation routes.
"""

from fastapi import APIRouter

from nexa_runtime.ai.router import router as ai_router
from nexa_runtime.auth.router import router as auth_router
from nexa_runtime.chat.router import router as chat_router
from nexa_runtime.memory.router import router as memory_router
from nexa_runtime.rag.router import router as rag_router
from nexa_runtime.tools.router import router as tools_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(ai_router)
api_router.include_router(chat_router)
api_router.include_router(memory_router)
api_router.include_router(rag_router)
api_router.include_router(tools_router)
