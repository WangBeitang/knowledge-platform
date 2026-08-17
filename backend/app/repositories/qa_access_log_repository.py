"""qa_access_logs 数据访问：每个接受处理的 Turn 恰好一条日志。"""

from datetime import datetime

from sqlalchemy import select, update

from app.models.qa_access_log import QaAccessLog
from app.repositories.base import BaseRepository

# FAQ 候选聚合的数量上限（冻结数据对象 §4.9：样例/来源日志"限制数量"）
SAMPLE_QUESTIONS_LIMIT = 5
SOURCE_LOG_IDS_LIMIT = 50

# 知识缺口 reason_code（冻结数据对象 §4.12）
GAP_REASON_NO_CITATION = "no_citation"
GAP_REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"


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

    async def aggregate_gap_groups(self) -> list[dict]:
        """按 (knowledge_scope, normalized_question_hash) 聚合知识缺口日志（GapService 使用）。

        冻结规则（数据对象 §4.12 / 模块 SPEC §13）：
        - 仅「RAG 正常完成的成功 Turn」进入分析：status=succeeded 且 error_code 为空；
        - answer_source 必须为 rag（FAQ 精确命中 faq_cache 是标准答案，不是缺口）；
        - 产生缺口的日志：terminal_reason_code=insufficient_evidence（上游明确证据不足）
          或 citation_count=0（正常完成但无引用 → no_citation）；
        - 鉴权失败、RAG 不可用、timeout、系统异常、失败 Turn（status=failed /
          error_code 非空）一律不产生缺口；
        - 组内 reason 优先级：存在 insufficient_evidence 日志取 insufficient_evidence，
          否则 no_citation（同一归一化问题可能同时出现两种日志）；
        - ask_count = 组内缺口日志条数；有引用的正常日志不计入。
        """
        from app.rag.scope_policy import VALID_SCOPES

        rows = (await self.session.execute(select(QaAccessLog))).scalars().all()
        groups: dict[tuple[str, str], dict] = {}
        for log in rows:
            # 非成功 Turn / 带错误码（RAG_UNAVAILABLE、timeout、系统异常等）不产生缺口
            if log.status != "succeeded" or log.error_code is not None:
                continue
            # 仅 RAG 正常完成的问答进入分析；FAQ 精确命中不算知识缺口
            if log.answer_source != "rag":
                continue
            is_insufficient = log.terminal_reason_code == GAP_REASON_INSUFFICIENT_EVIDENCE
            if not is_insufficient and log.citation_count > 0:
                continue
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
                    "reason_code": GAP_REASON_NO_CITATION,
                },
            )
            group["ask_count"] += 1
            if is_insufficient:
                group["reason_code"] = GAP_REASON_INSUFFICIENT_EVIDENCE
            if log.question and log.question not in group["sample_questions"]:
                group["sample_questions"].append(log.question[:500])
            if log.id and len(group["source_log_ids"]) < SOURCE_LOG_IDS_LIMIT:
                group["source_log_ids"].append(log.id)
            if len(group["sample_questions"]) > SAMPLE_QUESTIONS_LIMIT:
                group["sample_questions"] = group["sample_questions"][:SAMPLE_QUESTIONS_LIMIT]
        # 排序稳定：ask_count 降序，hash 升序
        items = sorted(
            groups.values(),
            key=lambda g: (-g["ask_count"], g["normalized_question_hash"]),
        )
        return items
