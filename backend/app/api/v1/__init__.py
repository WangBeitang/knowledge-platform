"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import auth, chat, chunks, documents, health, integration, users

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(integration.router)
router.include_router(documents.router)
router.include_router(chunks.router)
router.include_router(chat.router)
