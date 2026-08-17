"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    chat,
    chunks,
    documents,
    faq_candidates,
    faq_sync_runs,
    faqs,
    health,
    integration,
    users,
)

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(integration.router)
router.include_router(documents.router)
router.include_router(chunks.router)
router.include_router(chat.router)
router.include_router(faq_candidates.router)
router.include_router(faqs.router)
router.include_router(faq_sync_runs.router)
