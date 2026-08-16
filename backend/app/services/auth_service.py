"""认证服务：登录认证、会话撤销、令牌校验、当前用户视图。

鉴权事实（冻结 §3.1）：JWT 只证明令牌真实性；每次请求重新检查签名、过期、
会话存在且未撤销、用户存在且 active、iat 不早于最近密码变更时间，角色以数据库为准。
"""

from datetime import timedelta
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    is_iat_before_password_change,
    jti_hash,
    new_jti,
    verify_password,
)
from app.core.time import utc_now_naive
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import LoginUserView, MeView

# 业务异常：登录失败统一 401 AUTH_REQUIRED（冻结错误码表）
AUTH_FAILED = AppError("AUTH_REQUIRED", "用户名或密码错误", status_code=401)
ACCOUNT_DISABLED = AppError("AUTH_REQUIRED", "账号已停用", status_code=401)
TOKEN_INVALID = AppError("AUTH_REQUIRED", "登录状态无效或已过期，请重新登录", status_code=401)


def _login_user_view(user: User) -> LoginUserView:
    return LoginUserView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
    )


def me_view(user: User) -> MeView:
    from app.core.time import iso8601

    return MeView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=iso8601(user.created_at),
        last_login_at=iso8601(user.last_login_at),
    )


class AuthService:
    def __init__(
        self,
        user_repository: UserRepository,
        session_repository: AuthSessionRepository,
    ) -> None:
        self.users = user_repository
        self.sessions = session_repository

    async def authenticate_user(
        self,
        *,
        username: str,
        password: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, AuthSession, str, int]:
        """用户名密码登录：校验 → 建立会话 → 签发 JWT。失败抛 401 AUTH_REQUIRED。"""
        normalized = username.strip().lower()
        if not normalized or not password:
            raise AUTH_FAILED
        user = await self.users.get_by_username(normalized)
        if user is None or not verify_password(password, user.password_hash):
            raise AUTH_FAILED
        if user.status != "active":
            raise ACCOUNT_DISABLED

        settings = get_settings()
        now = utc_now_naive()
        jti = new_jti()
        expires_at = now + timedelta(seconds=settings.jwt_expires_seconds)
        session = await self.sessions.create_session(
            user_id=user.id,
            jti_hash=jti_hash(jti),
            issued_at=now,
            expires_at=expires_at,
            client_ip=client_ip,
            user_agent=(user_agent or "")[:500] or None,
        )
        token, expires_in = create_access_token(
            user_id=user.id,
            session_id=session.id,
            jti=jti,
            role=user.role,
            expires_seconds=settings.jwt_expires_seconds,
        )
        await self.users.touch_login(user, now)
        return user, session, token, expires_in

    async def revoke_session(self, session_id: str) -> bool:
        """撤销当前会话（登出）。幂等：已撤销返回 False 表示无新变化。"""
        return await self.sessions.revoke(session_id, utc_now_naive())

    async def validate_token(self, payload: dict[str, Any]) -> User:
        """内部受保护请求的完整鉴权链，通过则返回数据库中的当前用户。

        校验顺序（任一失败 → 401 AUTH_REQUIRED）：
        签名/过期（decode 层已做）→ 会话存在且未撤销 → jti 匹配 →
        用户存在且 active → iat 不早于最近密码变更时间。
        """
        sub = payload.get("sub")
        sid = payload.get("sid")
        jti = payload.get("jti")
        iat = payload.get("iat")
        if not sub or not sid or not jti or not isinstance(iat, int):
            raise TOKEN_INVALID

        session = await self.sessions.get_by_id(sid)
        if session is None or session.revoked_at is not None:
            raise TOKEN_INVALID
        if session.user_id != sub or session.jti_hash != jti_hash(jti):
            raise TOKEN_INVALID

        user = await self.users.get_by_id(sub)
        if user is None or user.status != "active":
            raise TOKEN_INVALID
        # 同秒边界：JWT iat 秒级 + 会话签发时间微秒级，共同判定是否早于最近密码变更
        if is_iat_before_password_change(iat, session.issued_at, user.password_changed_at):
            raise TOKEN_INVALID
        return user
