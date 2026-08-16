"""document_replacements：新文档成功后替换旧文档的审计关系。"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class DocumentReplacement(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_replacements"

    old_managed_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_documents.id", ondelete="RESTRICT"), nullable=False
    )
    new_managed_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_documents.id", ondelete="RESTRICT"), nullable=False
    )
    replacement_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
