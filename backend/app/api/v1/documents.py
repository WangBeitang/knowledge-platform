"""文档管理路由（仅管理员）：导入/列表/详情/重建/替换/删除（《API 接口设计》§7）。"""

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.document_replacement_repository import DocumentReplacementRepository
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.rag_integration_task_repository import RagIntegrationTaskRepository
from app.schemas.document import (
    DeleteData,
    DeleteResponse,
    DocumentDetailResponse,
    DocumentImportData,
    DocumentImportResponse,
    DocumentListData,
    DocumentListResponse,
    RebuildData,
    RebuildResponse,
    ReplaceData,
    ReplaceResponse,
)
from app.services.audit_service import AuditService
from app.services.document_service import DocumentService

router = APIRouter(prefix="/admin/documents", tags=["documents"])


def _document_service(session: AsyncSession) -> DocumentService:
    return DocumentService(
        docs=ManagedDocumentRepository(session),
        tasks=RagIntegrationTaskRepository(session),
        replacements=DocumentReplacementRepository(session),
        audit=AuditService(AuditLogRepository(session)),
    )


async def _client_ip(request) -> str | None:
    return request.client.host if request.client else None


@router.post("/import", response_model=DocumentImportResponse)
async def import_documents(
    request: Request,
    knowledge_scope: str = Form(...),
    files: list[UploadFile] = File(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DocumentImportResponse:
    file_list = []
    for upload in files or []:
        raw = await upload.read()
        file_list.append((upload.filename or "", raw))
    data = await _document_service(session).import_documents(
        operator=admin,
        knowledge_scope=knowledge_scope,
        files=file_list,
        client_ip=await _client_ip(request),
    )
    return DocumentImportResponse(
        request_id=get_request_id(),
        data=DocumentImportData(
            knowledge_scope=data.knowledge_scope,
            submitted_count=data.submitted_count,
            rejected_count=data.rejected_count,
            items=data.items,
        ),
    )


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_scope: str | None = Query(default=None),
    platform_status: str | None = Query(default=None),
    file_name: str | None = Query(default=None),
    source_kind: str | None = Query(default=None),
    sort_by: str = Query(default="updated_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    items, total = await _document_service(session).list_documents(
        page=page,
        page_size=page_size,
        knowledge_scope=knowledge_scope,
        platform_status=platform_status,
        file_name=file_name,
        source_kind=source_kind,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return DocumentListResponse(
        request_id=get_request_id(),
        data=DocumentListData(items=items, page=page, page_size=page_size, total=total),
    )


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    document_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DocumentDetailResponse:
    view = await _document_service(session).get_document(document_id)
    return DocumentDetailResponse(request_id=get_request_id(), data=view)


@router.post("/{document_id}/rebuild", status_code=202, response_model=RebuildResponse)
async def rebuild_document(
    request: Request,
    document_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> RebuildResponse:
    task_id, doc_id, _operation = await _document_service(session).rebuild_document(
        operator=admin,
        document_id=document_id,
        client_ip=await _client_ip(request),
    )
    return RebuildResponse(
        request_id=get_request_id(),
        data=RebuildData(task_id=task_id, document_id=doc_id),
    )


@router.delete("/{document_id}", response_model=DeleteResponse)
async def delete_document(
    request: Request,
    document_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DeleteResponse:
    deleted_id = await _document_service(session).delete_document(
        operator=admin,
        document_id=document_id,
        client_ip=await _client_ip(request),
    )
    return DeleteResponse(
        request_id=get_request_id(),
        data=DeleteData(id=deleted_id, platform_status="deleted"),
    )


@router.post("/{document_id}/replace", status_code=202, response_model=ReplaceResponse)
async def replace_document(
    request: Request,
    document_id: str,
    knowledge_scope: str = Form(...),
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ReplaceResponse:
    raw = await file.read()
    task_id, new_doc_id, replacement_id = await _document_service(session).replace_document(
        operator=admin,
        document_id=document_id,
        knowledge_scope=knowledge_scope,
        file_name=file.filename or "",
        file_bytes=raw,
        client_ip=await _client_ip(request),
    )
    return ReplaceResponse(
        request_id=get_request_id(),
        data=ReplaceData(
            task_id=task_id,
            new_document_id=new_doc_id,
            replacement_id=replacement_id,
            status="pending",
        ),
    )
