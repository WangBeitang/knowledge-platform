"""qa_access_logs 数据访问：每个接受处理的 Turn 恰好一条日志。"""

from datetime import datetime

from sqlalchemy import select, update

from app.models.qa_access_log import QaAccessLog
from app.repositories.base import BaseRepository

# FAQ 候选聚合的数量上限（冻结数据对象 §4.9：样例/来源日志"限制数量"）
SAMPLE_QUESTIONS_LIMIT = 5
SOURCE_LOG_IDS_LIMIT = 50


class QaAccessLogRepository(BaseRepository[QaAccessLog]):
    model = QaAccessLog

    async def create_log(
        self,
        *,
        turn_id: str,
        session_id: str,
        channel: str,
        user_id: str | None,
        external_subject_hash: str | None,
        question: str,
        normalized_question: str,
        normalized_question_hash: str,
        allowed_scopes_json: list,
        answer_source: str,
        faq_id: str | None,
        rag_trace_id: str | None,
        terminal_reason_code: str | None,
        citation_count: int,
        citation_document_ids_json: list,
        latency_ms: int,
        status: str,
        error_code: str | None,
        created_at: datetime,
    ) -> QaAccessLog:
        log = QaAccessLog(
            turn_id=turn_id,
            session_id=session_id,
            channel=channel,
            user_id=user_id,
            external_subject_hash=external_subject_hash,
            question=question,
            normalized_question=normalized_question,
            normalized_question_hash=normalized_question_hash,
            allowed_scopes_json=allowed_scopes_json,
            answer_source=answer_source,
            faq_id=faq_id,
            rag_trace_id=rag_trace_id,
            terminal_reason_code=terminal_reason_code,
            citation_count=citation_count,
            citation_document_ids_json=citation_document_ids_json,
            # Token 初始为 null，由 RAG Trace 补取回填；不可获取时保持 null，不填 0。
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms=latency_ms,
            status=status,
            error_code=error_code,
            created_at=created_at,
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def update_tokens(
        self,
        *,
        turn_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        """Trace 补取成功后只更新对应 turn 的 Token 字段（available=false 时保持 null）。"""
        await self.session.execute(
            update(QaAccessLog)
            .where(QaAccessLog.turn_id == turn_id)
            .values(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        )

    async def aggregate_by_hash(self) -> list[dict]:
        """按 (knowledge_scope, normalized_question_hash) 聚合日志（FaqAnalysisService 使用）。

        第一版只按归一化精确值聚合（冻结数据对象 §4.9：不声称语义聚类）：
        - knowledge_scope 取每条日志 allowed_scopes_json 的第一优先级范围
          （scopes_for_role 的顺序即优先级，平台集中计算，不在 SQL 中拼装）；
        - ask_count = 组内日志条数；
        - sample_questions 取组内去重后的原始问题（最多 SAMPLE_QUESTIONS_LIMIT 条）；
        - source_log_ids 取组内日志 id（最多 SOURCE_LOG_IDS_LIMIT 条）。
        """
        from app.rag.scope_policy import VALID_SCOPES

        rows = (await self.session.execute(select(QaAccessLog))).scalars().all()
        groups: dict[tuple[str, str], dict] = {}
        for log in rows:
            scopes = [
                s
                for s in (log.allowed_scopes_json or [])
                if isinstance(s, str) and s in VALID_SCOPES
            ]
            if not scopes:
                continue
            scope = scopes[0]  # 优先级最高（admin_private > internal_shared > external_public）
            key = (scope, log.normalized_question_hash)
            group = groups.setdefault(
                key,
                {
                    "knowledge_scope": scope,
                    "normalized_question": log.normalized_question,
                    "normalized_question_hash": log.normalized_question_hash,
                    "ask_count": 0,
                    "sample_questions": [],
                    "source_log_ids": [],
                },
            )
            group["ask_count"] += 1
            if log.question and log.question not in group["sample_questions"]:
                group["sample_questions"].append(log.question[:500])
            if log.id and len(group["source_log_ids"]) < SOURCE_LOG_IDS_LIMIT:
                group["source_log_ids"].append(log.id)
            # 样例问题与来源日志数量上限
            if len(group["sample_questions"]) > SAMPLE_QUESTIONS_LIMIT:
                group["sample_questions"] = group["sample_questions"][:SAMPLE_QUESTIONS_LIMIT]
        # 排序稳定：ask_count 降序，hash 升序
        items = sorted(
            groups.values(),
            key=lambda g: (-g["ask_count"], g["normalized_question_hash"]),
        )
        return items
