"""faqs：管理员审核后的正式 FAQ（MySQL 为事实来源）。"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Faq(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "faqs"
    __table_args__ = (
        Index("ix_faqs_scope_hash", "knowledge_scope", "normalized_question_hash", unique=True),
        Index("ix_faqs_scope_status", "knowledge_scope", "status"),
    )

    knowledge_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    answer: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("faq_candidates.id", ondelete="RESTRICT"), nullable=True
    )
    hit_count: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False, default=0)
    rag_sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    rag_sync_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reviewed_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    unpublished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
