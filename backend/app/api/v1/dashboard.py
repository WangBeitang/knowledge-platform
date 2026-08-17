"""运营看板路由（仅管理员）（《API 接口设计》§13.2）。

冻结接口：
- GET /admin/dashboard/summary        PV/UV/问答量/成功率/延迟/Token + coverage_rate
- GET /admin/dashboard/trends         按日或小时趋势（granularity=day|hour）
- GET /admin/dashboard/top-questions  高频问题（limit <= 100）
- GET /admin/dashboard/top-documents  高频引用文档（limit <= 100）

通用过滤：date_from / date_to / channel。数据来自 qa_access_logs 真实日志。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.errors import AppError
from app.core.request_context import get_request_id
from app.core.time import parse_utc_bound
from app.models.user import User
from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.dashboard import (
    DASHBOARD_RANK_LIMIT_MAX,
    DashboardSummaryResponse,
    DashboardTrendsResponse,
    TopDocumentResponse,
    TopQuestionResponse,
)
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])


def _dashboard_service(session: AsyncSession) -> DashboardService:
    return DashboardService(
        logs=QaAccessLogRepository(session),
        documents=ManagedDocumentRepository(session),
    )


def _parse_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    """日期参数解析：非法值统一 422（INVALID_REQUEST），不进入 SQL。"""
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


@router.get("/summary", response_model=DashboardSummaryResponse)
async def summary(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DashboardSummaryResponse:
    data = await _dashboard_service(session).get_summary(
        date_from=_parse_bound(date_from, end_of_day=False),
        date_to=_parse_bound(date_to, end_of_day=True),
        channel=channel,
    )
    return DashboardSummaryResponse(request_id=get_request_id(), data=data)


@router.get("/trends", response_model=DashboardTrendsResponse)
async def trends(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    granularity: str = Query(default="day", pattern="^(day|hour)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> DashboardTrendsResponse:
    data = await _dashboard_service(session).get_trends(
        date_from=_parse_bound(date_from, end_of_day=False),
        date_to=_parse_bound(date_to, end_of_day=True),
        channel=channel,
        granularity=granularity,
    )
    return DashboardTrendsResponse(request_id=get_request_id(), data=data)


@router.get("/top-questions", response_model=TopQuestionResponse)
async def top_questions(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=DASHBOARD_RANK_LIMIT_MAX),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> TopQuestionResponse:
    items = await _dashboard_service(session).get_top_questions(
        date_from=_parse_bound(date_from, end_of_day=False),
        date_to=_parse_bound(date_to, end_of_day=True),
        channel=channel,
        limit=limit,
    )
    return TopQuestionResponse(request_id=get_request_id(), data=items)


@router.get("/top-documents", response_model=TopDocumentResponse)
async def top_documents(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=DASHBOARD_RANK_LIMIT_MAX),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> TopDocumentResponse:
    items = await _dashboard_service(session).get_top_documents(
        date_from=_parse_bound(date_from, end_of_day=False),
        date_to=_parse_bound(date_to, end_of_day=True),
        channel=channel,
        limit=limit,
    )
    return TopDocumentResponse(request_id=get_request_id(), data=items)
