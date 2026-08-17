"""知识缺口服务：管理员手动 analyze / ignore / resolve（冻结 §13.1 / 数据对象 §4.12）。

规则（冻结）：
- 仅管理员手动触发，非定时、非问答链路自动生成；
- 来源只允许 qa_access_logs（本服务只读，不修改 Stage 4 问答链路）；
- 仅 RAG 正常完成的成功 Turn 进入分析：no_citation（无引用）或
  insufficient_evidence（上游明确证据不足）；鉴权失败、RAG 不可用、timeout、
  系统异常、失败 Turn 不得产生缺口；
- 按 knowledge_scope + normalized_question_hash 聚合；
- 重复 analyze 遵循去重语义：同 scope+同 hash 覆盖式更新统计，不重复建行；
- ignore / resolve 只允许 pending_review → 终态（状态机冻结 §7.4），
  已处理候选不因重复 analyze 复活；写现有 AuditService。
"""

from typing import Any

from app.core.enums import AuditAction, KnowledgeGapStatus
from app.core.errors import conflict, not_found
from app.core.time import utc_now_naive
from app.models.user import User
from app.repositories.knowledge_gap_candidate_repository import (
    KnowledgeGapCandidateRepository,
)
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.gap import GapView
from app.services.audit_service import AuditService


def gap_view(candidate) -> GapView:
    """ORM → 响应 DTO（仅暴露冻结字段，JSON 列由 DB 反序列化）。"""
    return GapView(
        id=candidate.id,
        knowledge_scope=candidate.knowledge_scope,
        normalized_question=candidate.normalized_question,
        normalized_question_hash=candidate.normalized_question_hash,
        sample_questions=candidate.sample_questions_json or [],
        ask_count=candidate.ask_count,
        reason_code=candidate.reason_code,
        status=candidate.status,
        resolution_note=candidate.resolution_note,
        resolved_document_id=candidate.resolved_document_id,
        reviewed_by_user_id=candidate.reviewed_by_user_id,
        created_at=candidate.created_at,
        last_seen_at=candidate.last_seen_at,
        reviewed_at=candidate.reviewed_at,
    )


class GapService:
    def __init__(
        self,
        *,
        logs: QaAccessLogRepository,
        gaps: KnowledgeGapCandidateRepository,
        audit: AuditService,
    ) -> None:
        self.logs = logs
        self.gaps = gaps
        self.audit = audit

    async def analyze_logs(self) -> dict[str, int]:
        """全量聚合缺口日志并覆盖式 upsert 候选。

        返回统计：created（新建候选数）/ updated（覆盖更新数）。
        """
        groups = await self.logs.aggregate_gap_groups()
        created = 0
        updated = 0
        for group in groups:
            existed = await self.gaps.get_by_scope_hash(
                knowledge_scope=group["knowledge_scope"],
                normalized_question_hash=group["normalized_question_hash"],
            )
            await self.gaps.upsert_aggregate(
                knowledge_scope=group["knowledge_scope"],
                normalized_question=group["normalized_question"],
                normalized_question_hash=group["normalized_question_hash"],
                ask_count=group["ask_count"],
                sample_questions=group["sample_questions"],
                source_log_ids=group["source_log_ids"],
                reason_code=group["reason_code"],
                first_seen_at=group["first_seen_at"],
                last_seen_at=group["last_seen_at"],
            )
            if existed is None:
                created += 1
            else:
                updated += 1
        return {"created": created, "updated": updated}

    async def list_gaps(
        self,
        *,
        page: int,
        page_size: int,
        knowledge_scope: str | None,
        status: str | None,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[GapView], int]:
        rows, total = await self.gaps.list_page(
            page=page,
            page_size=page_size,
            knowledge_scope=knowledge_scope,
            status=status,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return [gap_view(row) for row in rows], total

    async def ignore_gap(
        self, *, gap_id: str, operator: User, client_ip: str | None
    ) -> GapView:
        candidate = await self.gaps.get_by_id(gap_id)
        if candidate is None:
            raise not_found("知识缺口候选不存在")
        if candidate.status != KnowledgeGapStatus.pending_review.value:
            raise conflict("候选已处理，不能重复操作")
        now = utc_now_naive()
        before = self._snapshot(candidate)
        await self.gaps.mark_ignored(
            candidate, reviewed_by_user_id=operator.id, reviewed_at=now
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.gap_ignored.value,
            resource_type="knowledge_gap",
            resource_id=candidate.id,
            result="succeeded",
            before=before,
            after=self._snapshot(candidate),
            client_ip=client_ip,
        )
        return gap_view(candidate)

    async def resolve_gap(
        self,
        *,
        gap_id: str,
        resolution_note: str | None,
        resolved_document_id: str | None,
        operator: User,
        client_ip: str | None,
    ) -> GapView:
        candidate = await self.gaps.get_by_id(gap_id)
        if candidate is None:
            raise not_found("知识缺口候选不存在")
        if candidate.status != KnowledgeGapStatus.pending_review.value:
            raise conflict("候选已处理，不能重复操作")
        now = utc_now_naive()
        before = self._snapshot(candidate)
        await self.gaps.mark_resolved(
            candidate,
            resolution_note=resolution_note,
            resolved_document_id=resolved_document_id,
            reviewed_by_user_id=operator.id,
            reviewed_at=now,
        )
        await self.audit.record(
            operator_user_id=operator.id,
            action=AuditAction.gap_resolved.value,
            resource_type="knowledge_gap",
            resource_id=candidate.id,
            result="succeeded",
            before=before,
            after=self._snapshot(candidate),
            client_ip=client_ip,
        )
        return gap_view(candidate)

    @staticmethod
    def _snapshot(candidate) -> dict[str, Any]:
        """审计安全快照：只含非敏感候选字段。"""
        return {
            "knowledge_scope": candidate.knowledge_scope,
            "normalized_question_hash": candidate.normalized_question_hash,
            "reason_code": candidate.reason_code,
            "status": candidate.status,
            "ask_count": candidate.ask_count,
            "resolution_note": candidate.resolution_note,
            "resolved_document_id": candidate.resolved_document_id,
        }
