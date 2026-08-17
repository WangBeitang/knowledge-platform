"""FAQ 同步记录路由（仅管理员）：列表（查询时刷新上游任务状态）。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.faq_repository import FaqRepository
from app.repositories.faq_sync_run_repository import FaqSyncRunRepository
from app.schemas.faq import FaqSyncRunListData, FaqSyncRunListResponse
from app.services.faq_sync_service import FaqSyncService

router = APIRouter(prefix="/admin/faq-sync-runs", tags=["faq-sync-runs"])


def _sync_service(session: AsyncSession) -> FaqSyncService:
    return FaqSyncService(
        runs=FaqSyncRunRepository(session),
        faqs=FaqRepository(session),
    )


@router.get("", response_model=FaqSyncRunListResponse)
async def list_sync_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_scope: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqSyncRunListResponse:
    items, total = await _sync_service(session).list_sync_runs(
        page=page,
        page_size=page_size,
        knowledge_scope=knowledge_scope,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
        operator_user_id=admin.id,
    )
    return FaqSyncRunListResponse(
        request_id=get_request_id(),
        data=FaqSyncRunListData(items=items, page=page, page_size=page_size, total=total),
    )
