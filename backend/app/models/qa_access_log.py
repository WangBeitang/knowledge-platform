"""qa_access_logs：每次问答轮次的运营与看板事实。"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class QaAccessLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "qa_access_logs"
    __table_args__ = (
        Index("ix_qa_turn_id", "turn_id", unique=True),
        Index("ix_qa_channel_created", "channel", "created_at"),
        Index("ix_qa_hash_created", "normalized_question_hash", "created_at"),
        Index("ix_qa_rag_trace_id", "rag_trace_id"),
    )

    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    external_subject_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_scopes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    answer_source: Mapped[str] = mapped_column(String(20), nullable=False)
    faq_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rag_trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    citation_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    citation_document_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    input_tokens: Mapped[int | None] = mapped_column(INTEGER(unsigned=True), nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(INTEGER(unsigned=True), nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(INTEGER(unsigned=True), nullable=True)
    latency_ms: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
