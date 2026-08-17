"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    audit_logs,
    auth,
    chat,
    chunks,
    dashboard,
    documents,
    faq_candidates,
    faq_sync_runs,
    faqs,
    health,
    integration,
    knowledge_gaps,
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
router.include_router(knowledge_gaps.router)
router.include_router(dashboard.router)
router.include_router(audit_logs.router)
