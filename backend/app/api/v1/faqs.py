"""正式 FAQ 管理路由（仅管理员）：列表/创建/更新/下线/重发/同步重试（《API 接口设计》§12）。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.errors import not_found
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.faq_sync_run_repository import FaqSyncRunRepository
from app.schemas.faq import (
    FaqCreateRequest,
    FaqDetailResponse,
    FaqListData,
    FaqListResponse,
    FaqSyncRunView,
    FaqUpdateRequest,
)
from app.services.audit_service import AuditService
from app.services.faq_service import FaqService
from app.services.faq_sync_service import FaqSyncService, sync_run_view

router = APIRouter(prefix="/admin/faqs", tags=["faqs"])


def _faq_service(session: AsyncSession) -> FaqService:
    faqs_repo = FaqRepository(session)
    return FaqService(
        repository=faqs_repo,
        candidates=None,
        audit=AuditService(AuditLogRepository(session)),
        sync_service=FaqSyncService(
            runs=FaqSyncRunRepository(session),
            faqs=faqs_repo,
        ),
    )


def _sync_service(session: AsyncSession) -> FaqSyncService:
    return FaqSyncService(
        runs=FaqSyncRunRepository(session),
        faqs=FaqRepository(session),
    )


async def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("", response_model=FaqListResponse)
async def list_faqs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_scope: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort_by: str = Query(default="updated_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqListResponse:
    items, total = await _faq_service(session).list_faqs(
        page=page,
        page_size=page_size,
        knowledge_scope=knowledge_scope,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return FaqListResponse(
        request_id=get_request_id(),
        data=FaqListData(items=items, page=page, page_size=page_size, total=total),
    )


@router.post("", response_model=FaqDetailResponse)
async def create_faq(
    request: Request,
    body: FaqCreateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqDetailResponse:
    view = await _faq_service(session).create_faq(
        knowledge_scope=body.knowledge_scope,
        question=body.question,
        answer=body.answer,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqDetailResponse(request_id=get_request_id(), data=view)


@router.patch("/{faq_id}", response_model=FaqDetailResponse)
async def update_faq(
    request: Request,
    faq_id: str,
    body: FaqUpdateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqDetailResponse:
    view = await _faq_service(session).update_faq(
        faq_id=faq_id,
        question=body.question,
        answer=body.answer,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqDetailResponse(request_id=get_request_id(), data=view)


@router.post("/{faq_id}/unpublish", response_model=FaqDetailResponse)
async def unpublish_faq(
    request: Request,
    faq_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqDetailResponse:
    view = await _faq_service(session).unpublish_faq(
        faq_id=faq_id,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqDetailResponse(request_id=get_request_id(), data=view)


@router.post("/{faq_id}/publish", response_model=FaqDetailResponse)
async def republish_faq(
    request: Request,
    faq_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqDetailResponse:
    view = await _faq_service(session).republish_faq(
        faq_id=faq_id,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqDetailResponse(request_id=get_request_id(), data=view)


class SyncRetryData(FaqSyncRunView):
    """sync:retry 返回同步记录视图（前端以 faq-sync-runs 轮询为准）。"""


@router.post("/{faq_id}/sync:retry", response_model=FaqSyncRunView)
async def retry_sync(
    request: Request,
    faq_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqSyncRunView:
    """重试对应范围 RAG 同步：按 FAQ 的 scope 定位最新同步记录。"""
    faq = await FaqRepository(session).get_by_id(faq_id)
    if faq is None:
        raise not_found("FAQ 不存在")
    run_repo = FaqSyncRunRepository(session)
    latest = await run_repo.find_latest_by_scope(faq.knowledge_scope)
    if latest is None:
        raise not_found("该范围暂无同步记录")
    run = await _sync_service(session).retry_sync(
        run_id=latest.id,
        operator_user_id=admin.id,
    )
    # 同步重试写操作审计（冻结 API §13.3）
    from app.core.enums import AuditAction

    await AuditService(AuditLogRepository(session)).record(
        operator_user_id=admin.id,
        action=AuditAction.faq_sync_retried.value,
        resource_type="faq_sync_run",
        resource_id=run.id,
        result="succeeded",
        after={
            "knowledge_scope": run.knowledge_scope,
            "status": run.status,
            "content_hash": run.content_hash,
        },
        client_ip=await _client_ip(request),
    )
    return sync_run_view(run)
