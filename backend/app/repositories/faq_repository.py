"""faqs 数据访问：正式 FAQ（MySQL 为事实来源）。

包含 Stage 4 最小精确命中查询 + Stage 5 管理查询/写操作。
只负责 SQL，禁止调用 RAG / Redis。
"""

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.faq import Faq
from app.repositories.base import BaseRepository

# FAQ 列表可排序白名单（冻结契约：禁止任意列排序）
FAQ_SORT_WHITELIST = {"created_at", "updated_at", "published_at", "hit_count"}


class FaqRepository(BaseRepository[Faq]):
    model = Faq

    async def find_published_by_scope_hash(
        self,
        *,
        knowledge_scope: str,
        normalized_question_hash: str,
    ) -> Faq | None:
        """按 scope + 归一化哈希精确查询正式 FAQ（只允许 published）。"""
        stmt = select(Faq).where(
            Faq.knowledge_scope == knowledge_scope,
            Faq.normalized_question_hash == normalized_question_hash,
            Faq.status == "published",
        )
        return await self.session.scalar(stmt)

    async def increment_hit_count(self, faq_id: str) -> None:
        """成功交付 FAQ 后原子自增命中数（数据库原子更新，不读改写）。"""
        await self.session.execute(
            update(Faq).where(Faq.id == faq_id).values(hit_count=Faq.hit_count + 1)
        )

    async def find_by_scope_hash(
        self, *, knowledge_scope: str, normalized_question_hash: str
    ) -> Faq | None:
        """按 scope + hash 查询（任意状态，供创建前去重/同步状态更新）。"""
        stmt = select(Faq).where(
            Faq.knowledge_scope == knowledge_scope,
            Faq.normalized_question_hash == normalized_question_hash,
        )
        return await self.session.scalar(stmt)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_scope: str | None = None,
        status: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[Faq], int]:
        if sort_by not in FAQ_SORT_WHITELIST:
            sort_by = "updated_at"
        column = getattr(self.model, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if knowledge_scope:
            conditions.append(Faq.knowledge_scope == knowledge_scope)
        if status:
            conditions.append(Faq.status == status)

        count_stmt = select(func.count()).select_from(Faq).where(*conditions)
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(Faq)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def create_faq(
        self,
        *,
        id_value: str | None,
        knowledge_scope: str,
        question: str,
        normalized_question: str,
        normalized_question_hash: str,
        answer: str,
        status: str,
        source_candidate_id: str | None,
        hit_count: int,
        rag_sync_status: str,
        rag_sync_error: str | None,
        created_by_user_id: str,
        reviewed_by_user_id: str,
        published_at: datetime,
        updated_at: datetime,
        unpublished_at: datetime | None,
    ) -> Faq:
        faq = Faq(
            id=id_value or None,
            knowledge_scope=knowledge_scope,
            question=question,
            normalized_question=normalized_question,
            normalized_question_hash=normalized_question_hash,
            answer=answer,
            status=status,
            source_candidate_id=source_candidate_id,
            hit_count=hit_count,
            rag_sync_status=rag_sync_status,
            rag_sync_error=rag_sync_error,
            created_by_user_id=created_by_user_id,
            reviewed_by_user_id=reviewed_by_user_id,
            published_at=published_at,
            updated_at=updated_at,
            unpublished_at=unpublished_at,
        )
        self.session.add(faq)
        await self.session.flush()
        return faq

    async def update_faq_fields(
        self,
        faq: Faq,
        *,
        question: str,
        normalized_question: str,
        normalized_question_hash: str,
        answer: str,
        updated_at: datetime,
    ) -> None:
        """PATCH 更新完整可变字段：问题（重算归一化）与答案。"""
        faq.question = question
        faq.normalized_question = normalized_question
        faq.normalized_question_hash = normalized_question_hash
        faq.answer = answer
        faq.updated_at = updated_at
        await self.session.flush()

    async def set_status(
        self,
        faq: Faq,
        *,
        status: str,
        updated_at: datetime,
        unpublished_at: datetime | None = None,
    ) -> None:
        """发布/下线状态切换（下线不物理删除）。"""
        faq.status = status
        faq.updated_at = updated_at
        faq.unpublished_at = unpublished_at
        await self.session.flush()

    async def set_rag_sync_status(
        self,
        faq: Faq,
        *,
        rag_sync_status: str,
        rag_sync_error: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """更新该 FAQ 所在范围的 RAG 文档同步状态（冗余展示字段，事实在 faq_sync_runs）。"""
        faq.rag_sync_status = rag_sync_status
        if rag_sync_error is not None:
            faq.rag_sync_error = rag_sync_error[:1000] if rag_sync_error else None
        if updated_at is not None:
            faq.updated_at = updated_at
        await self.session.flush()

    async def update_scope_sync_status(
        self,
        *,
        knowledge_scope: str,
        rag_sync_status: str,
        rag_sync_error: str | None = None,
        updated_at: datetime,
    ) -> None:
        """批量更新某 scope 全部 published FAQ 的 RAG 同步状态（同步事件触发时调用）。"""
        values: dict = {"rag_sync_status": rag_sync_status, "updated_at": updated_at}
        if rag_sync_error is not None:
            values["rag_sync_error"] = rag_sync_error[:1000] if rag_sync_error else None
        await self.session.execute(
            update(Faq)
            .where(
                Faq.knowledge_scope == knowledge_scope,
                Faq.status == "published",
            )
            .values(**values)
        )
