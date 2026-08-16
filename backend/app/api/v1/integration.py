"""RAG 集成路由（仅管理员）：状态查询与三档 Dataset bootstrap（《API 接口设计》§6）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.integration import (
    BootstrapData,
    BootstrapRequest,
    BootstrapResponse,
    RagStatusData,
    RagStatusResponse,
)
from app.services.audit_service import AuditService
from app.services.bootstrap_service import BootstrapService, is_import_base_url_configured

router = APIRouter(prefix="/admin/integration", tags=["integration"])


def _bootstrap_service(session: AsyncSession) -> BootstrapService:
    return BootstrapService(
        audit_service=AuditService(AuditLogRepository(session)),
    )


@router.get("/rag/status", response_model=RagStatusResponse)
async def rag_status(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> RagStatusResponse:
    items, overall = await _bootstrap_service(session).get_rag_status()
    return RagStatusResponse(
        request_id=get_request_id(),
        data=RagStatusData(
            import_base_url_configured=is_import_base_url_configured(),
            datasets=items,
            overall=overall,
        ),
    )


@router.post("/rag/bootstrap", response_model=BootstrapResponse)
async def rag_bootstrap(
    payload: BootstrapRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> BootstrapResponse:
    items, overall, _result = await _bootstrap_service(session).bootstrap_datasets(
        verify_only=payload.verify_only,
        operator_user_id=admin.id,
    )
    return BootstrapResponse(
        request_id=get_request_id(),
        data=BootstrapData(verify_only=payload.verify_only, datasets=items, overall=overall),
    )
