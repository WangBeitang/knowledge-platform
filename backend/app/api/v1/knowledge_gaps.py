"""知识缺口管理路由（仅管理员）：analyze / 列表 / ignore / resolve（《API 接口设计》§13.1）。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.knowledge_gap_candidate_repository import (
    KnowledgeGapCandidateRepository,
)
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.gap import (
    GapAnalyzeData,
    GapAnalyzeResponse,
    GapDetailResponse,
    GapListData,
    GapListResponse,
    GapResolveRequest,
)
from app.services.audit_service import AuditService
from app.services.gap_service import GapService

router = APIRouter(prefix="/admin/knowledge-gaps", tags=["knowledge-gaps"])


def _gap_service(session: AsyncSession) -> GapService:
    return GapService(
        logs=QaAccessLogRepository(session),
        gaps=KnowledgeGapCandidateRepository(session),
        audit=AuditService(AuditLogRepository(session)),
    )


async def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/analyze", response_model=GapAnalyzeResponse)
async def analyze_gaps(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GapAnalyzeResponse:
    stats = await _gap_service(session).analyze_logs()
    return GapAnalyzeResponse(
        request_id=get_request_id(),
        data=GapAnalyzeData(created=stats["created"], updated=stats["updated"]),
    )


@router.get("", response_model=GapListResponse)
async def list_gaps(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_scope: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort_by: str = Query(default="last_seen_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GapListResponse:
    items, total = await _gap_service(session).list_gaps(
        page=page,
        page_size=page_size,
        knowledge_scope=knowledge_scope,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return GapListResponse(
        request_id=get_request_id(),
        data=GapListData(items=items, page=page, page_size=page_size, total=total),
    )


@router.post("/{gap_id}/ignore", response_model=GapDetailResponse)
async def ignore_gap(
    request: Request,
    gap_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GapDetailResponse:
    view = await _gap_service(session).ignore_gap(
        gap_id=gap_id,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return GapDetailResponse(request_id=get_request_id(), data=view)


@router.post("/{gap_id}/resolve", response_model=GapDetailResponse)
async def resolve_gap(
    request: Request,
    gap_id: str,
    body: GapResolveRequest | None = None,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> GapDetailResponse:
    view = await _gap_service(session).resolve_gap(
        gap_id=gap_id,
        resolution_note=body.resolution_note if body else None,
        resolved_document_id=body.resolved_document_id if body else None,
        operator=admin,
        client_ip=await _client_ip(request),
    )
    return GapDetailResponse(request_id=get_request_id(), data=view)
