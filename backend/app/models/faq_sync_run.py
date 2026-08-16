"""faq_sync_runs：每个知识范围 FAQ Markdown 文档的同步执行记录。"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class FaqSyncRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "faq_sync_runs"

    knowledge_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    rag_task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rag_document_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    previous_rag_document_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    requested_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
