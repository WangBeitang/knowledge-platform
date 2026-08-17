"""qa_access_logs 数据访问：每个接受处理的 Turn 恰好一条日志。"""

from datetime import datetime

from sqlalchemy import update

from app.models.qa_access_log import QaAccessLog
from app.repositories.base import BaseRepository


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
