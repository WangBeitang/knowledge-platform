"""knowledge_gap_candidates：轻量知识缺口候选（仅管理员手动 analyze 产生）。"""

from datetime import datetime

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.dialects.mysql import DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class KnowledgeGapCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "knowledge_gap_candidates"
    __table_args__ = (
        Index("ix_kgc_scope_hash", "knowledge_scope", "normalized_question_hash", unique=True),
    )

    knowledge_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_questions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_log_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ask_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resolved_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
