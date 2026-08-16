"""FastAPI 依赖：数据库会话、Redis、当前用户（认证）、管理员校验（授权）。

职责分离：
- `get_current_user`：authentication——完整鉴权链（JWT → 会话 → 用户状态 → 密码变更时间）；
- `require_admin`：authorization——叠加角色校验，employee 拒绝 403 PERMISSION_DENIED。

JWT 只证明令牌真实性，角色以数据库 `users.role` 为准（冻结 §3.1）。
"""

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.errors import forbidden
from app.core.redis import get_redis_dependency
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import TOKEN_INVALID, AuthService

__all__ = ["get_db", "get_redis_dependency", "get_current_user", "require_admin"]

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    """认证依赖：校验 Bearer JWT 并返回数据库中的当前用户。"""
    from app.core.security import decode_access_token

    if credentials is None or not credentials.credentials:
        raise TOKEN_INVALID
    try:
        payload = decode_access_token(credentials.credentials)
    except Exception:
        raise TOKEN_INVALID from None
    # 供登出等路由使用：当前请求的会话 ID（只撤销当前 sid）
    request.state.auth_session_id = payload.get("sid")
    service = AuthService(
        user_repository=UserRepository(session),
        session_repository=AuthSessionRepository(session),
    )
    return await service.validate_token(payload)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """授权依赖：仅管理员可通过；员工访问 /admin/* 返回 403 PERMISSION_DENIED。"""
    if user.role != "admin":
        raise forbidden("无权访问该资源")
    return user
