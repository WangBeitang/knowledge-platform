"""rag_integration_tasks：平台与原 RAG 之间的长操作映射。"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RagIntegrationTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "rag_integration_tasks"
    __table_args__ = (Index("ix_rit_status_updated", "status", "updated_at"),)

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    managed_document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("managed_documents.id", ondelete="RESTRICT"), nullable=True
    )
    rag_task_id: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    rag_document_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rag_dataset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    rag_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    done_nodes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    running_nodes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failed_node: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    requested_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
