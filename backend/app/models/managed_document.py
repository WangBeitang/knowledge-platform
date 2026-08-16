"""managed_documents：平台对原 RAG Document 的轻量映射（不存正文/Chunk）。"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.mysql import DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ManagedDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "managed_documents"
    __table_args__ = (
        Index("ix_md_scope_status_updated", "knowledge_scope", "platform_status", "updated_at"),
        Index("ix_md_dataset_rag_status", "rag_dataset_id", "rag_status"),
    )

    rag_document_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    rag_dataset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    knowledge_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    index_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=1)
    rag_status: Mapped[str] = mapped_column(String(40), nullable=False)
    rag_parse_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rag_index_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    platform_status: Mapped[str] = mapped_column(String(32), nullable=False)
    chunk_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    latest_rag_task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
