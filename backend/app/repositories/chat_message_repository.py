"""chat_messages 数据访问：终态一次性落库，不逐 delta 写库。"""

from sqlalchemy import func, select

from app.models.chat_message import ChatMessage
from app.repositories.base import BaseRepository


class ChatMessageRepository(BaseRepository[ChatMessage]):
    model = ChatMessage

    async def max_seq_no(self, session_id: str) -> int:
        stmt = select(func.max(ChatMessage.seq_no)).where(ChatMessage.session_id == session_id)
        value = await self.session.scalar(stmt)
        return int(value or 0)

    async def append_messages(self, messages: list[ChatMessage]) -> None:
        """一次性写入 user + assistant 两条消息（调用方需在事务内分配 seq_no）。"""
        for message in messages:
            self.session.add(message)
        await self.session.flush()

    async def list_by_session(self, session_id: str) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.seq_no.asc(), ChatMessage.created_at.asc())
        )
        return list((await self.session.scalars(stmt)).all())
