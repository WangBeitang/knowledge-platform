"""faq_candidates 数据访问（只负责 SQL，禁止调用 RAG / Redis）。

聚合结果由 FaqAnalysisService 计算后通过 `upsert` 覆盖式写入：
- 重复 analyze 同 scope+同 hash 时更新统计（ask_count/sample/source_log_ids），不重复建行；
- 已发布 FAQ（faqs 表存在 published）由服务层跳过，不进入本表。
"""

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.faq_candidate import FaqCandidate
from app.repositories.base import BaseRepository

# 候选列表可排序白名单（冻结契约：禁止任意列排序）
FAQ_CANDIDATE_SORT_WHITELIST = {"generated_at", "ask_count", "knowledge_scope"}


class FaqCandidateRepository(BaseRepository[FaqCandidate]):
    model = FaqCandidate

    async def get_by_scope_hash(
        self, *, knowledge_scope: str, normalized_question_hash: str
    ) -> FaqCandidate | None:
        stmt = select(FaqCandidate).where(
            FaqCandidate.knowledge_scope == knowledge_scope,
            FaqCandidate.normalized_question_hash == normalized_question_hash,
        )
        return await self.session.scalar(stmt)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_scope: str | None = None,
        status: str | None = None,
        sort_by: str = "generated_at",
        sort_order: str = "desc",
    ) -> tuple[list[FaqCandidate], int]:
        if sort_by not in FAQ_CANDIDATE_SORT_WHITELIST:
            sort_by = "generated_at"
        column = getattr(self.model, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if knowledge_scope:
            conditions.append(FaqCandidate.knowledge_scope == knowledge_scope)
        if status:
            conditions.append(FaqCandidate.status == status)

        count_stmt = select(func.count()).select_from(FaqCandidate).where(*conditions)
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(FaqCandidate)
            .where(*conditions)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self.session.scalars(stmt)).all())
        return rows, int(total)

    async def upsert_aggregate(
        self,
        *,
        knowledge_scope: str,
        normalized_question: str,
        normalized_question_hash: str,
        ask_count: int,
        sample_questions: list[str],
        source_log_ids: list[str],
        generated_at: datetime,
    ) -> FaqCandidate:
        """覆盖式 upsert：重复 analyze 更新统计，不重复建行（UNIQUE scope+hash）。

        status 保留既有值（rejected/published 不因重复 analyze 复活）。
        """
        existing = await self.get_by_scope_hash(
            knowledge_scope=knowledge_scope,
            normalized_question_hash=normalized_question_hash,
        )
        if existing is not None:
            existing.ask_count = ask_count
            existing.sample_questions_json = sample_questions
            existing.source_log_ids_json = source_log_ids
            existing.generated_at = generated_at
            await self.session.flush()
            return existing
        candidate = FaqCandidate(
            knowledge_scope=knowledge_scope,
            normalized_question=normalized_question,
            normalized_question_hash=normalized_question_hash,
            sample_questions_json=sample_questions,
            ask_count=ask_count,
            suggested_answer=None,
            source_log_ids_json=source_log_ids,
            status="pending_review",
            published_faq_id=None,
            generated_at=generated_at,
            reviewed_by_user_id=None,
            reviewed_at=None,
        )
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def mark_published(
        self,
        candidate: FaqCandidate,
        *,
        published_faq_id: str,
        reviewed_by_user_id: str,
        reviewed_at: datetime,
    ) -> None:
        candidate.status = "published"
        candidate.published_faq_id = published_faq_id
        candidate.reviewed_by_user_id = reviewed_by_user_id
        candidate.reviewed_at = reviewed_at
        await self.session.flush()

    async def mark_rejected(
        self,
        candidate: FaqCandidate,
        *,
        reviewed_by_user_id: str,
        reviewed_at: datetime,
    ) -> None:
        candidate.status = "rejected"
        candidate.reviewed_by_user_id = reviewed_by_user_id
        candidate.reviewed_at = reviewed_at
        await self.session.flush()

    async def update_ask_count(self, faq_candidate_id: str, ask_count: int) -> None:
        """发布成功后把候选统计合并进已发布 FAQ 时使用（仅更新自身统计）。"""
        await self.session.execute(
            update(FaqCandidate)
            .where(FaqCandidate.id == faq_candidate_id)
            .values(ask_count=ask_count)
        )
