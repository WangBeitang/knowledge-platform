"""qa_access_logs 数据访问：每个接受处理的 Turn 恰好一条日志。"""

from collections import Counter
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
        - ask_count = 组内缺口日志条数；有引用的正常日志不计入；
        - first_seen_at / last_seen_at = 组内缺口日志 created_at 的 MIN / MAX
          （真实发生时间，非 analyze 时刻；数据对象 §4.12：首次/最近发生时间）。
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
                    "first_seen_at": log.created_at,
                    "last_seen_at": log.created_at,
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
            # 时间语义：以真实缺口日志时间为准
            if log.created_at < group["first_seen_at"]:
                group["first_seen_at"] = log.created_at
            if log.created_at > group["last_seen_at"]:
                group["last_seen_at"] = log.created_at
        # 排序稳定：ask_count 降序，hash 升序
        items = sorted(
            groups.values(),
            key=lambda g: (-g["ask_count"], g["normalized_question_hash"]),
        )
        return items

    # ---- Stage 5 Batch 3：运营看板（《API 接口设计》§13.2）----
    # V1 冻结口径：无物化统计 Worker，直接基于现有 MySQL 查询 +
    # 进程内聚合（与 aggregate_by_hash / aggregate_gap_groups 同一模式）。
    # 时间边界由调用方解析为 UTC naive（qa_access_logs.created_at 为 UTC naive）。

    async def list_stats_logs(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        channel: str | None = None,
    ) -> list[QaAccessLog]:
        """按冻结通用过滤（date_from/date_to/channel）取回看板统计所需日志。"""
        conditions = []
        if date_from is not None:
            conditions.append(QaAccessLog.created_at >= date_from)
        if date_to is not None:
            conditions.append(QaAccessLog.created_at <= date_to)
        if channel:
            conditions.append(QaAccessLog.channel == channel)
        stmt = select(QaAccessLog).where(*conditions)
        return list((await self.session.scalars(stmt)).all())

    @staticmethod
    def _user_identity(log: QaAccessLog) -> str | None:
        """统一用户身份：内部用户优先，外部匿名走 hash；两者皆空视为匿名。"""
        if log.user_id:
            return f"u:{log.user_id}"
        if log.external_subject_hash:
            return f"e:{log.external_subject_hash}"
        return None

    @staticmethod
    def _summary_from_rows(rows: list[QaAccessLog]) -> dict:
        """单区间汇总（summary 与 trends 单桶共用同一口径）。

        冻结口径：
        - pv_count = 日志条数；uv_count = 独立用户身份数；
        - question_count = status=succeeded 轮次数；
        - success_rate / avg_latency_ms 空区间为 None（不伪造 0）；
        - Token 只累加真实回填值，全 NULL 返回 None；coverage_rate = 完整
          total_tokens 条数 / 总条数（空区间 None）。
        """
        total = len(rows)
        pv = total
        uv = len(
            {
                identity
                for identity in (QaAccessLogRepository._user_identity(r) for r in rows)
                if identity is not None
            }
        )
        succeeded = sum(1 for r in rows if r.status == "succeeded")
        if total == 0:
            return {
                "pv_count": 0,
                "uv_count": 0,
                "question_count": 0,
                "success_rate": None,
                "avg_latency_ms": None,
                "token_input_total": None,
                "token_output_total": None,
                "token_total": None,
                "token_coverage_rate": None,
            }
        token_input = [r.input_tokens for r in rows if r.input_tokens is not None]
        token_output = [r.output_tokens for r in rows if r.output_tokens is not None]
        token_total = [r.total_tokens for r in rows if r.total_tokens is not None]
        coverage = sum(1 for r in rows if r.total_tokens is not None) / total
        return {
            "pv_count": pv,
            "uv_count": uv,
            "question_count": succeeded,
            "success_rate": round(succeeded / total, 4),
            "avg_latency_ms": round(sum(r.latency_ms for r in rows) / total, 2),
            "token_input_total": sum(token_input) if token_input else None,
            "token_output_total": sum(token_output) if token_output else None,
            "token_total": sum(token_total) if token_total else None,
            "token_coverage_rate": round(coverage, 4),
        }

    async def aggregate_summary(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        channel: str | None = None,
    ) -> dict:
        """summary：区间内真实日志汇总（PV/UV/问答量/成功率/延迟/Token+coverage）。"""
        rows = await self.list_stats_logs(date_from=date_from, date_to=date_to, channel=channel)
        return self._summary_from_rows(rows)

    async def aggregate_by_time(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        channel: str | None = None,
        granularity: str = "day",
    ) -> list[dict]:
        """trends：按 UTC 日/小时聚合，只返回真实存在日志的桶（不填充空桶）。"""
        rows = await self.list_stats_logs(date_from=date_from, date_to=date_to, channel=channel)
        buckets: dict[str, list[QaAccessLog]] = {}
        for log in rows:
            if granularity == "hour":
                key = log.created_at.replace(minute=0, second=0, microsecond=0)
                bucket = key.isoformat() + "+00:00"
            else:
                bucket = log.created_at.date().isoformat()
            buckets.setdefault(bucket, []).append(log)
        items = []
        for bucket in sorted(buckets):
            stat = self._summary_from_rows(buckets[bucket])
            items.append(
                {
                    "bucket": bucket,
                    "pv_count": stat["pv_count"],
                    "uv_count": stat["uv_count"],
                    "question_count": stat["question_count"],
                    "success_rate": stat["success_rate"],
                    "avg_latency_ms": stat["avg_latency_ms"],
                    "token_total": stat["token_total"],
                    "token_coverage_rate": stat["token_coverage_rate"],
                }
            )
        return items

    async def aggregate_by_question(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        channel: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """top-questions：按归一化问题 hash 聚合真实问题日志，频次降序。

        - sample_question 取组内最近一次（created_at 最大）原始问题；
        - 排序稳定：ask_count 降序、normalized_question 升序。
        """
        rows = await self.list_stats_logs(date_from=date_from, date_to=date_to, channel=channel)
        groups: dict[str, dict] = {}
        for log in rows:
            group = groups.setdefault(
                log.normalized_question_hash,
                {
                    "normalized_question": log.normalized_question,
                    "sample_question": None,
                    "sample_created_at": None,
                    "ask_count": 0,
                },
            )
            group["ask_count"] += 1
            if group["sample_created_at"] is None or log.created_at > group["sample_created_at"]:
                group["sample_question"] = log.question[:500]
                group["sample_created_at"] = log.created_at
        items = sorted(
            groups.values(),
            key=lambda g: (-g["ask_count"], g["normalized_question"]),
        )
        for item in items:
            item.pop("sample_created_at", None)
        return items[:limit]

    async def aggregate_by_citation(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        channel: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """top-documents：展开真实 citation_document_ids_json 计数，引用频次降序。

        同一 Turn 的 Citation 文档 ID 已去重（chat_service.extract_citation_document_ids），
        每出现一次计 1 次引用；排序稳定：citation_count 降序、document_id 升序。
        """
        rows = await self.list_stats_logs(date_from=date_from, date_to=date_to, channel=channel)
        counter: Counter = Counter()
        for log in rows:
            for doc_id in log.citation_document_ids_json or []:
                if isinstance(doc_id, str) and doc_id:
                    counter[doc_id] += 1
        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"document_id": doc_id, "citation_count": count} for doc_id, count in ranked]
