"""chat_messages：会话消息与最终交付快照（流式增量不逐 Token 写库）。"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import DATETIME, INTEGER, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ChatMessage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_cm_session_seq", "session_id", "seq_no", unique=True),
        Index("ix_cm_turn_id", "turn_id"),
        Index("ix_cm_rag_trace_id", "rag_trace_id"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq_no: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    answer_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rag_trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    citations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
