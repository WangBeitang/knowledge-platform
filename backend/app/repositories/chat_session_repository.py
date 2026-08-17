"""chat_sessions 数据访问（只负责 SQL，不含业务规则）。"""

from datetime import datetime

from sqlalchemy import select, update

from app.core.time import utc_now_naive
from app.models.chat_session import ChatSession
from app.repositories.base import BaseRepository


class ChatSessionRepository(BaseRepository[ChatSession]):
    model = ChatSession

    async def create_session(
        self,
        *,
        channel: str,
        user_id: str | None,
        title: str,
        status: str,
        created_at: datetime,
    ) -> ChatSession:
        session = ChatSession(
            channel=channel,
            user_id=user_id,
            external_subject_hash=None,
            external_session_id=None,
            title=title,
            status=status,
            last_message_at=None,
            deleted_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(session)
        await self.session.flush()
        return session

    async def get_owned(self, session_id: str, user_id: str) -> ChatSession | None:
        """按 id + 归属查询（含 deleted 状态，是否拒绝由服务层决定）。
        返回 None 表示不存在或不属于当前用户，调用方统一 404。
        """
        stmt = select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
        )
        return await self.session.scalar(stmt)

    async def get_by_id(self, session_id: str) -> ChatSession | None:
        """按 id 查询（不限定归属；外部 API 会话 user_id 为空，用本方法定位）。"""
        return await self.session.get(ChatSession, session_id)

    # ---------- 外部 API 轻量会话映射（数据对象 §4.6） ----------

    async def find_external_session(
        self, external_session_id: str, external_subject_hash: str
    ) -> ChatSession | None:
        """按外部会话映射定位平台会话（非 deleted，取最近创建的一条）。

        `external_session_id` 只用于外部会话映射；平台会话 ID（UUID）作为上游
        RAG session_id，天然与内部员工会话隔离。
        """
        stmt = (
            select(ChatSession)
            .where(
                ChatSession.channel == "external_api",
                ChatSession.external_session_id == external_session_id,
                ChatSession.external_subject_hash == external_subject_hash,
                ChatSession.status != "deleted",
            )
            .order_by(ChatSession.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def create_external_session(
        self,
        *,
        external_session_id: str,
        external_subject_hash: str,
        title: str,
        status: str,
        created_at: datetime,
    ) -> ChatSession:
        """创建外部 API 会话：channel=external_api，user_id 恒为空，不绑定员工账号。"""
        session = ChatSession(
            channel="external_api",
            user_id=None,
            external_subject_hash=external_subject_hash,
            external_session_id=external_session_id,
            title=title,
            status=status,
            last_message_at=None,
            deleted_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(session)
        await self.session.flush()
        return session

    async def list_by_user(
        self,
        *,
        user_id: str,
        page: int,
        page_size: int,
    ) -> tuple[list[ChatSession], int]:
        """当前用户未软删除的会话，按最近消息时间倒序（无消息时按创建时间）。"""
        from sqlalchemy import case, func

        sort_expr = case(
            (ChatSession.last_message_at.is_(None), ChatSession.created_at),
            else_=ChatSession.last_message_at,
        )
        count_stmt = (
            select(func.count())
            .select_from(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.status != "deleted")
        )
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.status != "deleted")
            .order_by(sort_expr.desc(), ChatSession.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def update_session_fields(
        self,
        session: ChatSession,
        *,
        title: str | None = None,
        status: str | None = None,
    ) -> None:
        if title is not None:
            session.title = title
        if status is not None:
            session.status = status
        session.updated_at = utc_now_naive()
        await self.session.flush()

    async def soft_delete(
        self,
        session: ChatSession,
        *,
        deleted_at: datetime,
    ) -> None:
        session.status = "deleted"
        session.deleted_at = deleted_at
        session.updated_at = deleted_at
        await self.session.flush()

    async def touch_last_message(self, session_id: str, at: datetime) -> None:
        """终态写入后刷新最近消息时间（不更新 title/status）。"""
        await self.session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(last_message_at=at, updated_at=at)
        )

    async def get_for_update(self, session_id: str) -> ChatSession | None:
        """锁定会话行（终态持久化时用于并发安全分配 seq_no）。"""
        stmt = select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        return await self.session.scalar(stmt)
