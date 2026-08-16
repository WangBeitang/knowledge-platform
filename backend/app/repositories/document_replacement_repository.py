"""document_replacements 数据访问（只负责 SQL，禁止调用 RAG）。"""

from datetime import datetime

from sqlalchemy import select

from app.models.document_replacement import DocumentReplacement
from app.repositories.base import BaseRepository


class DocumentReplacementRepository(BaseRepository[DocumentReplacement]):
    model = DocumentReplacement

    async def get_by_task_id(self, task_id: str) -> DocumentReplacement | None:
        stmt = select(DocumentReplacement).where(DocumentReplacement.replacement_task_id == task_id)
        return await self.session.scalar(stmt)

    async def get_by_task_id_for_update(self, task_id: str) -> DocumentReplacement | None:
        """SELECT ... FOR UPDATE：replace 删除旧文档区只允许一个轮询进入。"""
        stmt = (
            select(DocumentReplacement)
            .where(DocumentReplacement.replacement_task_id == task_id)
            .with_for_update()
        )
        return await self.session.scalar(stmt)

    async def mark_completed(
        self, replacement: DocumentReplacement, *, completed_at: datetime
    ) -> None:
        replacement.status = "completed"
        replacement.completed_at = completed_at
        await self.session.flush()

    async def mark_failed(
        self, replacement: DocumentReplacement, *, error_message: str | None = None
    ) -> None:
        replacement.status = "failed"
        if error_message:
            replacement.error_message = error_message[:1000]
        await self.session.flush()
