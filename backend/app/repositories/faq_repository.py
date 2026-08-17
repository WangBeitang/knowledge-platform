"""faqs 数据访问：Stage 4 最小查询（published 精确命中 + hit_count 原子自增）。"""

from sqlalchemy import select, update

from app.models.faq import Faq
from app.repositories.base import BaseRepository


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
