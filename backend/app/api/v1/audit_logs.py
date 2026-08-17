"""审计日志查询路由（仅管理员）（《API 接口设计》§13.3）。

冻结接口：GET /admin/audit-logs。
必须可查到此前已接入的全部关键写操作：用户变更、Dataset bootstrap、
文档导入/重建/替换/删除、Chunk 启停、FAQ 管理与同步重试、
知识缺口 ignore/resolve。
响应禁止暴露密码、JWT、Service API Key、连接串、完整文档正文、模型隐藏推理。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.errors import AppError
from app.core.request_context import get_request_id
from app.core.time import parse_utc_bound
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit import AuditListData, AuditListResponse
from app.services.audit_service import AuditService

router = APIRouter(prefix="/admin/audit-logs", tags=["audit"])


def _parse_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return parse_utc_bound(value, end_of_day=end_of_day)
    except ValueError:
        raise AppError(
            "INVALID_REQUEST",
            "无效的日期参数，支持 YYYY-MM-DD 或 ISO 时间",
            status_code=422,
        ) from None


@router.get("", response_model=AuditListResponse)
async def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    operator_user_id: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    result: str | None = Query(default=None, pattern="^(succeeded|failed)$"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AuditListResponse:
    items, total = await AuditService(AuditLogRepository(session)).list_logs(
        page=page,
        page_size=page_size,
        action=action,
        operator_user_id=operator_user_id,
        resource_type=resource_type,
        result=result,
        date_from=_parse_bound(date_from, end_of_day=False),
        date_to=_parse_bound(date_to, end_of_day=True),
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return AuditListResponse(
        request_id=get_request_id(),
        data=AuditListData(items=items, page=page, page_size=page_size, total=total),
    )
