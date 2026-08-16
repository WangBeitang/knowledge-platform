"""managed_documents 数据访问（只负责 SQL，禁止调用 RAG）。"""

from sqlalchemy import select

from app.core.enums import ManagedDocumentStatus
from app.models.managed_document import ManagedDocument
from app.repositories.base import BaseRepository

# 文档列表可排序白名单（冻结契约：禁止任意列排序）
DOCUMENT_SORT_WHITELIST = {"created_at", "updated_at", "file_name"}


class ManagedDocumentRepository(BaseRepository[ManagedDocument]):
    model = ManagedDocument

    async def get_active(self, document_id: str) -> ManagedDocument | None:
        """获取非 deleted/replaced 的映射（详情/操作入口的默认可见范围）。"""
        stmt = select(ManagedDocument).where(
            ManagedDocument.id == document_id,
            ManagedDocument.platform_status.notin_(
                [ManagedDocumentStatus.deleted.value, ManagedDocumentStatus.replaced.value]
            ),
        )
        return await self.session.scalar(stmt)

    async def get_by_rag_document_id(self, rag_document_id: str) -> ManagedDocument | None:
        stmt = select(ManagedDocument).where(ManagedDocument.rag_document_id == rag_document_id)
        return await self.session.scalar(stmt)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_scope: str | None = None,
        platform_status: str | None = None,
        file_name: str | None = None,
        source_kind: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[ManagedDocument], int]:
        """文档列表：MySQL 查询，不逐行访问 RAG。

        默认不展示 deleted/replaced；显式按状态筛选时允许查看历史映射。
        """
        if sort_by not in DOCUMENT_SORT_WHITELIST:
            sort_by = "updated_at"
        column = getattr(self.model, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if knowledge_scope:
            conditions.append(ManagedDocument.knowledge_scope == knowledge_scope)
        if file_name:
            conditions.append(ManagedDocument.file_name.like(f"%{file_name}%"))
        if source_kind:
            conditions.append(ManagedDocument.source_kind == source_kind)
        if platform_status:
            conditions.append(ManagedDocument.platform_status == platform_status)
        else:
            conditions.append(
                ManagedDocument.platform_status.notin_(
                    [
                        ManagedDocumentStatus.deleted.value,
                        ManagedDocumentStatus.replaced.value,
                    ]
                )
            )

        from sqlalchemy import func

        count_stmt = select(func.count()).select_from(ManagedDocument).where(*conditions)
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(ManagedDocument)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def update_snapshot(self, doc: ManagedDocument) -> None:
        """刷新轻量快照（RAG 状态/版本/chunk 数等），更新时间戳由调用方设置。"""
        await self.session.flush()

    async def mark_deleted(self, doc: ManagedDocument, *, deleted_at) -> None:
        doc.platform_status = ManagedDocumentStatus.deleted.value
        doc.deleted_at = deleted_at
        await self.session.flush()

    async def mark_replaced(self, doc: ManagedDocument) -> None:
        doc.platform_status = ManagedDocumentStatus.replaced.value
        await self.session.flush()
