"""chat_sessions：平台内部员工会话；外部 API 也可保存轻量映射。"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    external_subject_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_message_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
