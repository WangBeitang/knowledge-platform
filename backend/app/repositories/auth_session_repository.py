"""auth_sessions 表数据访问：会话创建、按 sid 查询、撤销、按用户全撤销。"""

from datetime import datetime

from sqlalchemy import update

from app.models.auth_session import AuthSession
from app.repositories.base import BaseRepository


class AuthSessionRepository(BaseRepository[AuthSession]):
    model = AuthSession

    async def get_by_id(self, session_id: str) -> AuthSession | None:
        return await super().get_by_id(session_id)

    async def revoke(self, session_id: str, revoked_at: datetime) -> bool:
        """撤销单个会话（幂等）：已撤销/不存在均返回 False 表示无新变化。"""
        result = await self.session.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        await self.session.flush()
        return result.rowcount > 0

    async def revoke_all_for_user(self, user_id: str, revoked_at: datetime) -> int:
        """撤销某用户全部未撤销会话（重置密码/停用时使用），返回撤销条数。"""
        result = await self.session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        await self.session.flush()
        return result.rowcount or 0

    async def create_session(
        self,
        *,
        user_id: str,
        jti_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthSession:
        session = AuthSession(
            user_id=user_id,
            jti_hash=jti_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            client_ip=client_ip,
            user_agent=user_agent,
            created_at=issued_at,
        )
        return await self.add(session)
