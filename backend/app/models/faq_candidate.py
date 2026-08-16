"""faq_candidates：管理员手动触发日志分析后产生的 FAQ 候选。"""

from datetime import datetime

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.dialects.mysql import DATETIME, INTEGER, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class FaqCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "faq_candidates"
    __table_args__ = (
        Index("ix_fc_scope_hash", "knowledge_scope", "normalized_question_hash", unique=True),
    )

    knowledge_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_questions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ask_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    suggested_answer: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    source_log_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    published_faq_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
