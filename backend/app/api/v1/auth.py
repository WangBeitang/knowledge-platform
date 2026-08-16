"""认证路由：登录 / 登出 / 当前用户（《API 接口设计》§4.2）。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.request_context import get_request_id
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LoginUserView,
    LogoutResponse,
    MeResponse,
)
from app.services.auth_service import AuthService, me_view

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db),
) -> LoginResponse:
    service = AuthService(
        user_repository=UserRepository(session),
        session_repository=AuthSessionRepository(session),
    )
    user, _auth_session, token, expires_in = await service.authenticate_user(
        username=payload.username,
        password=payload.password,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return LoginResponse(
        request_id=get_request_id(),
        data={
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "user": LoginUserView(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                status=user.status,
            ),
        },
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> LogoutResponse:
    service = AuthService(
        user_repository=UserRepository(session),
        session_repository=AuthSessionRepository(session),
    )
    # 只撤销当前 sid 对应会话（冻结：不实现全设备退出）
    sid = request.state.auth_session_id
    if sid:
        await service.revoke_session(sid)
    return LogoutResponse(request_id=get_request_id(), data={"ok": True})


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(request_id=get_request_id(), data=me_view(user))


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or None
    return request.client.host[:64] if request.client else None
