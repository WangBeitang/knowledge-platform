"""knowledge_gap_candidates 数据访问（只负责 SQL，禁止调用 RAG / Redis）。

聚合结果由 GapService 计算后通过 `upsert_aggregate` 覆盖式写入：
- 重复 analyze 同 scope+同 hash 时更新统计（ask_count/sample/source_log_ids/
  last_seen_at/reason_code），不重复建行（UNIQUE scope+hash）；
- status 保留既有值：已 ignore/resolve 的候选不因重复 analyze 复活；
- ignore / resolve 只允许 pending_review → 终态（状态机冻结 §7.4）。
"""

from datetime import datetime

from sqlalchemy import func, select

from app.models.knowledge_gap_candidate import KnowledgeGapCandidate
from app.repositories.base import BaseRepository

# 候选列表可排序白名单（冻结契约：禁止任意列排序）
GAP_SORT_WHITELIST = {"created_at", "last_seen_at", "ask_count", "knowledge_scope"}


class KnowledgeGapCandidateRepository(BaseRepository[KnowledgeGapCandidate]):
    model = KnowledgeGapCandidate

    async def get_by_scope_hash(
        self, *, knowledge_scope: str, normalized_question_hash: str
    ) -> KnowledgeGapCandidate | None:
        stmt = select(KnowledgeGapCandidate).where(
            KnowledgeGapCandidate.knowledge_scope == knowledge_scope,
            KnowledgeGapCandidate.normalized_question_hash == normalized_question_hash,
        )
        return await self.session.scalar(stmt)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_scope: str | None = None,
        status: str | None = None,
        sort_by: str = "last_seen_at",
        sort_order: str = "desc",
    ) -> tuple[list[KnowledgeGapCandidate], int]:
        if sort_by not in GAP_SORT_WHITELIST:
            sort_by = "last_seen_at"
        column = getattr(self.model, sort_by)
        order = column.desc() if sort_order == "desc" else column.asc()

        conditions = []
        if knowledge_scope:
            conditions.append(KnowledgeGapCandidate.knowledge_scope == knowledge_scope)
        if status:
            conditions.append(KnowledgeGapCandidate.status == status)

        count_stmt = (
            select(func.count()).select_from(KnowledgeGapCandidate).where(*conditions)
        )
        total = await self.session.scalar(count_stmt) or 0
        stmt = (
            select(KnowledgeGapCandidate)
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
        reason_code: str,
        first_seen_at: datetime,
        last_seen_at: datetime,
    ) -> KnowledgeGapCandidate:
        """覆盖式 upsert：重复 analyze 更新统计，不重复建行（UNIQUE scope+hash）。

        时间语义（冻结数据对象 §4.12：首次/最近发生时间，来自真实缺口日志）：
        - 新行：created_at = 最早缺口日志时间，last_seen_at = 最晚缺口日志时间；
        - 已存在行：created_at 永远不改；last_seen_at 更新为真实最新缺口日志时间；
        - 重复 analyze 且无新缺口日志时，两个时间天然保持不变（取日志 MIN/MAX）；
        - reason_code 随组内日志更新（可能从 no_citation 变为 insufficient_evidence）；
        - status 保留既有值（ignored/resolved 不因重复 analyze 复活）。
        """
        existing = await self.get_by_scope_hash(
            knowledge_scope=knowledge_scope,
            normalized_question_hash=normalized_question_hash,
        )
        if existing is not None:
            existing.ask_count = ask_count
            existing.sample_questions_json = sample_questions
            existing.source_log_ids_json = source_log_ids
            existing.reason_code = reason_code
            existing.last_seen_at = last_seen_at
            await self.session.flush()
            return existing
        candidate = KnowledgeGapCandidate(
            knowledge_scope=knowledge_scope,
            normalized_question=normalized_question,
            normalized_question_hash=normalized_question_hash,
            sample_questions_json=sample_questions,
            source_log_ids_json=source_log_ids,
            ask_count=ask_count,
            reason_code=reason_code,
            status="pending_review",
            resolution_note=None,
            resolved_document_id=None,
            reviewed_by_user_id=None,
            created_at=first_seen_at,
            last_seen_at=last_seen_at,
            reviewed_at=None,
        )
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def mark_ignored(
        self,
        candidate: KnowledgeGapCandidate,
        *,
        reviewed_by_user_id: str,
        reviewed_at: datetime,
    ) -> None:
        candidate.status = "ignored"
        candidate.reviewed_by_user_id = reviewed_by_user_id
        candidate.reviewed_at = reviewed_at
        await self.session.flush()

    async def mark_resolved(
        self,
        candidate: KnowledgeGapCandidate,
        *,
        resolution_note: str | None,
        resolved_document_id: str | None,
        reviewed_by_user_id: str,
        reviewed_at: datetime,
    ) -> None:
        candidate.status = "resolved"
        candidate.resolution_note = resolution_note
        candidate.resolved_document_id = resolved_document_id
        candidate.reviewed_by_user_id = reviewed_by_user_id
        candidate.reviewed_at = reviewed_at
        await self.session.flush()
