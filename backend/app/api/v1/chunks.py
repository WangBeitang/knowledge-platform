"""Chunk 管理路由（仅管理员）：分页列表、详情、启停（《API 接口设计》§8）。

禁止 Chunk 正文编辑接口。
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.schemas.chunk import (
    ChunkDetailResponse,
    ChunkListData,
    ChunkListResponse,
    ChunkSetEnabledData,
    ChunkSetEnabledRequest,
    ChunkSetEnabledResponse,
)
from app.services.audit_service import AuditService
from app.services.chunk_service import ChunkService

router = APIRouter(prefix="/admin/documents", tags=["chunks"])


def _chunk_service(session: AsyncSession) -> ChunkService:
    return ChunkService(
        docs=ManagedDocumentRepository(session),
        audit=AuditService(AuditLogRepository(session)),
    )


@router.get("/{document_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    document_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ChunkListResponse:
    items, total, cur_page, cur_size = await _chunk_service(session).list_chunks(
        document_id=document_id, page=page, page_size=page_size
    )
    return ChunkListResponse(
        request_id=get_request_id(),
        data=ChunkListData(items=items, total=total, page=cur_page, page_size=cur_size),
    )


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ChunkDetailResponse)
async def get_chunk(
    document_id: str,
    chunk_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ChunkDetailResponse:
    view = await _chunk_service(session).get_chunk(document_id=document_id, chunk_id=chunk_id)
    return ChunkDetailResponse(request_id=get_request_id(), data=view)


@router.patch("/{document_id}/chunks/{chunk_id}/enabled", response_model=ChunkSetEnabledResponse)
async def set_chunk_enabled(
    request: Request,
    document_id: str,
    chunk_id: str,
    payload: ChunkSetEnabledRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ChunkSetEnabledResponse:
    result = await _chunk_service(session).set_enabled(
        operator=admin,
        document_id=document_id,
        chunk_id=chunk_id,
        enabled=payload.enabled,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
        expected_index_version=payload.expected_index_version,
        client_ip=request.client.host if request.client else None,
    )
    return ChunkSetEnabledResponse(
        request_id=get_request_id(),
        data=ChunkSetEnabledData(**result),
    )
