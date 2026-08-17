"""FAQ 候选管理路由（仅管理员）：analyze / 列表 / 拒绝 / 审核发布（《API 接口设计》§12）。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.faq_candidate_repository import FaqCandidateRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.faq_sync_run_repository import FaqSyncRunRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.faq import (
    AnalyzeData,
    AnalyzeResponse,
    FaqCandidateDetailResponse,
    FaqCandidateListData,
    FaqCandidateListResponse,
    FaqDetailResponse,
    FaqPublishRequest,
)
from app.services.audit_service import AuditService
from app.services.faq_analysis_service import FaqAnalysisService
from app.services.faq_service import FaqService
from app.services.faq_sync_service import FaqSyncService

router = APIRouter(prefix="/admin/faq-candidates", tags=["faq-candidates"])


def _faq_service(session: AsyncSession) -> FaqService:
    faqs_repo = FaqRepository(session)
    return FaqService(
        repository=faqs_repo,
        candidates=FaqCandidateRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        sync_service=FaqSyncService(
            runs=FaqSyncRunRepository(session),
            faqs=faqs_repo,
        ),
    )


async def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_candidates(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AnalyzeResponse:
    service = FaqAnalysisService(
        logs=QaAccessLogRepository(session),
        candidates=FaqCandidateRepository(session),
        faqs=FaqRepository(session),
    )
    stats = await service.analyze_logs()
    return AnalyzeResponse(
        request_id=get_request_id(),
        data=AnalyzeData(
            created=stats["created"],
            updated=stats["updated"],
            skipped_published=stats["skipped_published"],
        ),
    )


@router.get("", response_model=FaqCandidateListResponse)
async def list_candidates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_scope: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort_by: str = Query(default="generated_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqCandidateListResponse:
    items, total = await _faq_service(session).list_candidates(
        page=page,
        page_size=page_size,
        knowledge_scope=knowledge_scope,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return FaqCandidateListResponse(
        request_id=get_request_id(),
        data=FaqCandidateListData(items=items, page=page, page_size=page_size, total=total),
    )


@router.post("/{candidate_id}/reject", response_model=FaqCandidateDetailResponse)
async def reject_candidate(
    request: Request,
    candidate_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqCandidateDetailResponse:
    view = await _faq_service(session).reject_candidate(
        candidate_id=candidate_id,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqCandidateDetailResponse(request_id=get_request_id(), data=view)


@router.post("/{candidate_id}/publish", response_model=FaqDetailResponse)
async def publish_candidate(
    request: Request,
    candidate_id: str,
    body: FaqPublishRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> FaqDetailResponse:
    view = await _faq_service(session).publish_candidate(
        candidate_id=candidate_id,
        knowledge_scope=body.knowledge_scope,
        question=body.question,
        answer=body.answer,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return FaqDetailResponse(request_id=get_request_id(), data=view)
