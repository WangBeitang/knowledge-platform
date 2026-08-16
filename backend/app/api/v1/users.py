"""用户管理路由（仅管理员）：列表/创建/修改/重置密码（《API 接口设计》§5）。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ResetPasswordRequest,
    UserCreateRequest,
    UserListResponse,
    UserMessageResponse,
    UserUpdateRequest,
    UserViewResponse,
)
from app.services.audit_service import AuditService
from app.services.user_service import UserService

router = APIRouter(prefix="/admin/users", tags=["users"])


def _user_service(session: AsyncSession) -> UserService:
    return UserService(
        user_repository=UserRepository(session),
        session_repository=AuthSessionRepository(session),
        audit_service=AuditService(AuditLogRepository(session)),
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserListResponse:
    items, total = await _user_service(session).list_users(
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return UserListResponse(
        request_id=get_request_id(),
        data={"items": items, "page": page, "page_size": page_size, "total": total},
    )


@router.post("", response_model=UserViewResponse, status_code=201)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserViewResponse:
    view = await _user_service(session).create_user(
        operator=admin,
        username=payload.username,
        display_name=payload.display_name,
        role=payload.role,
        initial_password=payload.initial_password,
        client_ip=_client_ip(request),
    )
    return UserViewResponse(request_id=get_request_id(), data=view)


@router.patch("/{user_id}", response_model=UserViewResponse)
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserViewResponse:
    view = await _user_service(session).update_user(
        operator=admin,
        user_id=user_id,
        display_name=payload.display_name,
        role=payload.role,
        status=payload.status,
        client_ip=_client_ip(request),
    )
    return UserViewResponse(request_id=get_request_id(), data=view)


@router.post("/{user_id}/reset-password", response_model=UserMessageResponse)
async def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> UserMessageResponse:
    view = await _user_service(session).reset_password(
        operator=admin,
        user_id=user_id,
        new_password=payload.new_password,
        client_ip=_client_ip(request),
    )
    return UserMessageResponse(
        request_id=get_request_id(),
        data={"id": view.id, "message": "密码已重置，旧登录状态已失效"},
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or None
    return request.client.host[:64] if request.client else None
