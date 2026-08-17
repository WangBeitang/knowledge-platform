"""内部问答服务（Stage 4 纵向切片核心）。

职责：
- 会话 CRUD（只操作自己的，deleted 404，archived 禁止发问）；
- 问题归一化 → FAQ 精确短路（Redis → MySQL）→ 未命中调原 RAG；
- 平台 SSE 重封装（ready/progress/delta/final/error，终态唯一）；
- 终态一次性落库（user + assistant 共用 turn_id，seq_no 严格递增）；
- qa_access_logs 每 Turn 恰好一条；
- 同 Session 禁止并发 Turn（进程内 registry，finally 释放）；
- RAG final 后 BackgroundTasks 补取 Trace 真实 Token。

安全边界（冻结）：
- role/scope/dataset/service user 全部由当前 DB 用户角色计算，绝不信任 JWT role；
- Citation 只保存白名单安全字段，缺失即 null，不伪造；
- 浏览器断开 ≠ 任务失败，不得把业务 Turn 标为 RAG failed；
- 禁止保存上游异常堆栈 / 内部节点名 / 路径 / dataset id。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import BackgroundTasks

from app.core.enums import AnswerSource, MessageRole, MessageStatus, SessionStatus
from app.core.errors import conflict, not_found
from app.core.normalizer import question_hash
from app.core.time import utc_now_naive
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.rag.rag_errors import RagError, rag_bad_response
from app.rag.rag_query_client import RagQueryClient
from app.rag.rag_trace_client import RagTraceClient
from app.rag.scope_policy import dataset_ids_for_role, scopes_for_role, service_user_for_role
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.chat_session_repository import ChatSessionRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.chat import CitationView
from app.services.audit_service import AuditService
from app.services.faq_service import FaqService

logger = logging.getLogger("app.services.chat_service")

DEFAULT_SESSION_TITLE = "新会话"
DEFAULT_TITLE_MAX_LEN = 30

# ---- SSE 事件名 ----
EV_READY = "ready"
EV_PROGRESS = "progress"
EV_DELTA = "delta"
EV_FINAL = "final"
EV_ERROR = "error"

# progress.stage 只允许这四个（冻结 API §11）
STAGE_FAQ_LOOKUP = "faq_lookup"
STAGE_RAG_SUBMIT = "rag_submit"
STAGE_RAG_PROGRESS = "rag_progress"
STAGE_FINALIZING = "finalizing"

# 平台错误码（稳定、可安全展示）
ERR_RAG_UNAVAILABLE = "RAG_UNAVAILABLE"
ERR_RAG_TIMEOUT = "RAG_TIMEOUT"
ERR_RAG_BAD_RESPONSE = "RAG_BAD_RESPONSE"

_SAFE_RAG_ERROR_MESSAGES = {
    ERR_RAG_UNAVAILABLE: "知识检索服务暂时不可用，请稍后重试",
    ERR_RAG_TIMEOUT: "知识检索服务响应超时，请稍后重试",
    ERR_RAG_BAD_RESPONSE: "知识检索服务未能完成本次回答，请稍后重试",
}

# Citation raw 白名单：禁止本地路径/secret/key/prompt/隐藏推理/stack trace/内部配置
_ALLOWED_RAW_LOCAL_KEYS = ("source_type", "title", "score")
_ALLOWED_RAW_WEB_KEYS = ("source_type", "title", "score", "source")


class ActiveTurnRegistry:
    """进程内按 session_id 的活跃 Turn 注册表（单机单 API worker 假设）。

    - 只用于“同 session 同一时间一个进行中的问答”约束；
    - 不引入 Redis 锁 / 分布式锁 / busy 数据库字段；
    - 终态（final/error）后必须释放，防止 registry 无限增长。
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    async def try_acquire(self, session_id: str) -> bool:
        async with self._lock:
            if session_id in self._active:
                return False
            self._active.add(session_id)
            return True

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._active.discard(session_id)

    def active_count(self) -> int:
        return len(self._active)


_active_turns = ActiveTurnRegistry()


@dataclass
class TurnOutcome:
    """一次问答的终态事实（用于消息/日志持久化）。"""

    success: bool
    answer: str = ""
    answer_source: str = AnswerSource.none.value
    faq_id: str | None = None
    rag_trace_id: str | None = None
    terminal_reason_code: str | None = None
    citations: list[CitationView] = field(default_factory=list)
    error_code: str | None = None


def to_citation_view(raw: dict[str, Any]) -> CitationView:
    """把上游 Citation 安全投影为平台 CitationView（Stage 4 决策十六）。"""
    source_type = str(raw.get("source_type") or "")
    score = raw.get("score")
    score_float: float | None = None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        score_float = float(score)
    title = str(raw.get("title") or "")
    document_name = title or None

    if source_type == "web":
        source_url: str | None = None
        raw_source = raw.get("source")
        if isinstance(raw_source, str) and raw_source.strip():
            source_url = raw_source.strip()
        return CitationView(
            document_id=None,
            chunk_id=None,
            document_name=document_name,
            content_preview=None,
            score=score_float,
            source_url=source_url,
            index_version=None,
            raw={k: raw.get(k) for k in _ALLOWED_RAW_WEB_KEYS if raw.get(k) is not None},
        )

    # local
    document_id = raw.get("document_id")
    chunk_id = raw.get("chunk_id")
    return CitationView(
        document_id=str(document_id) if document_id else None,
        chunk_id=str(chunk_id) if chunk_id is not None else None,
        document_name=document_name,
        content_preview=None,
        score=score_float,
        source_url=None,
        index_version=None,
        raw={k: raw.get(k) for k in _ALLOWED_RAW_LOCAL_KEYS if raw.get(k) is not None},
    )


def extract_citation_document_ids(citations: list[CitationView]) -> list[str]:
    """只保存真实非空 document_id，去重保序。"""
    seen: set[str] = set()
    result: list[str] = []
    for citation in citations:
        if citation.document_id:
            if citation.document_id not in seen:
                seen.add(citation.document_id)
                result.append(citation.document_id)
    return result


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _safe_rag_error_code(exc: RagError) -> str:
    """RagError → 平台稳定错误码（不泄漏上游异常字符串）。"""
    if exc.code == ERR_RAG_TIMEOUT:
        return ERR_RAG_TIMEOUT
    if exc.code == ERR_RAG_UNAVAILABLE:
        return ERR_RAG_UNAVAILABLE
    return ERR_RAG_BAD_RESPONSE


def _error_sse(request_id: str, turn_id: str, code: str, retryable: bool = True) -> str:
    return _sse(
        EV_ERROR,
        {
            "request_id": request_id,
            "turn_id": turn_id,
            "code": code,
            "message": _SAFE_RAG_ERROR_MESSAGES.get(code, "知识检索服务暂时不可用，请稍后重试"),
            "retryable": retryable,
        },
    )


def auto_title(normalized_question: str) -> str:
    """用首个归一化问题生成默认标题（不调用 LLM）。"""
    title = normalized_question.strip()
    if len(title) > DEFAULT_TITLE_MAX_LEN:
        title = title[:DEFAULT_TITLE_MAX_LEN] + "…"
    return title or DEFAULT_SESSION_TITLE


class ChatService:
    def __init__(
        self,
        *,
        sessions: ChatSessionRepository,
        messages: ChatMessageRepository,
        logs: QaAccessLogRepository,
        faq_repository: FaqRepository,
        audit: AuditService,
        faq_service: FaqService | None = None,
        query_client: RagQueryClient | None = None,
        trace_client: RagTraceClient | None = None,
    ) -> None:
        self.sessions = sessions
        self.messages = messages
        self.logs = logs
        self.audit = audit
        self.faq_service = faq_service or FaqService(faq_repository)
        self.query_client = query_client
        self.trace_client = trace_client

    # ---------- 会话 CRUD ----------

    async def create_session(self, user: User) -> ChatSession:
        now = utc_now_naive()
        chat_session = await self.sessions.create_session(
            channel="internal_web",
            user_id=user.id,
            title=DEFAULT_SESSION_TITLE,
            status=SessionStatus.active.value,
            created_at=now,
        )
        await self.sessions.session.commit()
        return chat_session

    async def list_sessions(
        self, user: User, *, page: int, page_size: int
    ) -> tuple[list[ChatSession], int]:
        return await self.sessions.list_by_user(user_id=user.id, page=page, page_size=page_size)

    async def get_session_detail(self, user: User, session_id: str) -> ChatSession:
        chat_session = await self.sessions.get_owned(session_id, user.id)
        if chat_session is None or chat_session.status == SessionStatus.deleted.value:
            raise not_found("会话不存在")
        return chat_session

    async def update_session(
        self,
        user: User,
        session_id: str,
        *,
        title: str | None,
        status: str | None,
        client_ip: str | None,
    ) -> ChatSession:
        chat_session = await self.sessions.get_owned(session_id, user.id)
        if chat_session is None or chat_session.status == SessionStatus.deleted.value:
            raise not_found("会话不存在")
        before = {"title": chat_session.title, "status": chat_session.status}
        await self.sessions.update_session_fields(chat_session, title=title, status=status)
        await self.audit.record(
            operator_user_id=user.id,
            action="chat_session_updated",
            resource_type="chat_session",
            resource_id=chat_session.id,
            before=before,
            after={"title": chat_session.title, "status": chat_session.status},
            client_ip=client_ip,
        )
        await self.sessions.session.commit()
        return chat_session

    async def delete_session(
        self,
        user: User,
        session_id: str,
        *,
        client_ip: str | None,
    ) -> None:
        chat_session = await self.sessions.get_owned(session_id, user.id)
        if chat_session is None or chat_session.status == SessionStatus.deleted.value:
            raise not_found("会话不存在")
        await self.sessions.soft_delete(chat_session, deleted_at=utc_now_naive())
        await self.audit.record(
            operator_user_id=user.id,
            action="chat_session_deleted",
            resource_type="chat_session",
            resource_id=chat_session.id,
            before={"status": "active"},
            after={"status": "deleted"},
            client_ip=client_ip,
        )
        await self.sessions.session.commit()

    # ---------- 流式问答 ----------

    async def assert_can_stream(self, session_id: str, user_id: str) -> ChatSession:
        """发问前校验（响应开始前执行，404/409 以普通 JSON 返回）。"""
        chat_session = await self.sessions.get_owned(session_id, user_id)
        if chat_session is None or chat_session.status == SessionStatus.deleted.value:
            raise not_found("会话不存在")
        if chat_session.status == SessionStatus.archived.value:
            raise conflict("会话已归档，无法继续提问")
        return chat_session

    async def try_acquire_turn(self, session_id: str) -> bool:
        return await _active_turns.try_acquire(session_id)

    async def release_turn(self, session_id: str) -> None:
        await _active_turns.release(session_id)

    async def stream_answer(
        self,
        *,
        user: User,
        chat_session: ChatSession,
        question: str,
        normalized: str,
        request_id: str,
        background_tasks: BackgroundTasks,
        started_at: float,
    ) -> AsyncIterator[str]:
        """平台 SSE 事件流（调用方负责 acquire/release 与异常兜底）。"""
        turn_id = str(uuid.uuid4())
        session_id = chat_session.id
        question_hash_value = question_hash(normalized)
        scopes = scopes_for_role(user.role)

        # ready
        yield _sse(
            EV_READY,
            {"request_id": request_id, "turn_id": turn_id, "session_id": session_id},
        )
        # progress(faq_lookup)
        yield _progress(request_id, turn_id, STAGE_FAQ_LOOKUP, "正在检索常见问题")

        # FAQ 精确短路
        faq_hit = await self.faq_service.lookup_exact_faq(
            scopes=scopes,
            normalized_question=normalized,
            normalized_question_hash=question_hash_value,
        )
        if faq_hit is not None:
            outcome = TurnOutcome(
                success=True,
                answer=faq_hit.answer,
                answer_source=AnswerSource.faq_cache.value,
                faq_id=faq_hit.faq_id,
                citations=[],
            )
            await self._persist_turn(
                chat_session=chat_session,
                user=user,
                question=question,
                normalized=normalized,
                question_hash=question_hash_value,
                scopes=scopes,
                turn_id=turn_id,
                outcome=outcome,
                started_at=started_at,
            )
            # FAQ 命中禁止调用原 RAG；交付成功后显式记录命中
            try:
                await self.faq_service.record_hit(faq_hit.faq_id)
            except Exception:  # noqa: BLE001 命中计数失败不影响已交付答案
                logger.warning("FAQ hit_count 自增失败 faq_id=%s", faq_hit.faq_id)
            yield _final_sse(request_id, turn_id, outcome)
            return

        # FAQ 未命中 → 原 RAG
        rag_service_user = service_user_for_role(user.role)
        dataset_ids = dataset_ids_for_role(user.role)
        yield _progress(request_id, turn_id, STAGE_RAG_SUBMIT, "正在提交知识检索")
        try:
            await self.query_client.submit_query(
                query=question,
                session_id=session_id,
                dataset_ids=dataset_ids,
                service_user=rag_service_user,
            )
        except RagError as exc:
            outcome = TurnOutcome(success=False, error_code=_safe_rag_error_code(exc))
            await self._persist_turn(
                chat_session=chat_session,
                user=user,
                question=question,
                normalized=normalized,
                question_hash=question_hash_value,
                scopes=scopes,
                turn_id=turn_id,
                outcome=outcome,
                started_at=started_at,
            )
            yield _error_sse(request_id, turn_id, outcome.error_code)
            return

        yield _progress(request_id, turn_id, STAGE_RAG_PROGRESS, "正在检索知识库并生成答案")

        answer_parts: list[str] = []
        citations_raw: list[dict[str, Any]] = []
        trace_id: str | None = None
        terminal_reason: str | None = None
        error_code: str | None = None
        saw_terminal = False

        try:
            async for event, data in self.query_client.stream_events(
                session_id, service_user=rag_service_user
            ):
                if saw_terminal:
                    # 终态后上游多余事件：忽略，不重复发 final/error
                    continue
                if event == EV_READY:
                    continue  # 平台已发 ready，不重复
                if event == EV_PROGRESS:
                    # 不暴露内部 LangGraph 节点名/Planner 细节/路径
                    yield _progress(
                        request_id, turn_id, STAGE_RAG_PROGRESS, "正在检索知识库并生成答案"
                    )
                elif event == EV_DELTA:
                    text = data.get("delta")
                    if isinstance(text, str) and text:
                        answer_parts.append(text)
                        # 上游 {"delta": "..."} → 平台 {"text": "..."}
                        yield _sse(
                            EV_DELTA,
                            {"request_id": request_id, "turn_id": turn_id, "text": text},
                        )
                elif event == EV_FINAL:
                    answer_text = str(data.get("answer") or "")
                    upstream_trace_id = str(data.get("trace_id") or "").strip()
                    if not upstream_trace_id:
                        raise rag_bad_response("上游 final 缺少 trace_id")
                    answer_parts = [answer_text] if answer_text else answer_parts
                    trace_id = upstream_trace_id or None
                    citations_raw = list(data.get("citations") or [])
                    terminal_raw = data.get("terminal_reason_code")
                    terminal_reason = str(terminal_raw) if terminal_raw is not None else None
                    saw_terminal = True
                    break
                elif event == EV_ERROR:
                    # 上游显式 error：不直接透传上游异常字符串
                    error_code = ERR_RAG_BAD_RESPONSE
                    saw_terminal = True
                    break
                else:
                    continue  # 未知事件：不破坏流
        except RagError as exc:
            error_code = _safe_rag_error_code(exc)
            saw_terminal = False

        # 上游 SSE 未给出终态 → 有限兜底查 /status
        if not saw_terminal and error_code is None:
            yield _progress(request_id, turn_id, STAGE_FINALIZING, "正在整理结果")
            fallback = await self._fallback_to_status(session_id, rag_service_user)
            if fallback is None:
                error_code = ERR_RAG_BAD_RESPONSE
            else:
                answer_parts = [fallback["answer"]] if fallback["answer"] else answer_parts
                trace_id = fallback["trace_id"]
                citations_raw = fallback["citations"]
                terminal_reason = fallback["terminal_reason_code"]
                saw_terminal = True

        success = saw_terminal and error_code is None
        outcome = TurnOutcome(
            success=success,
            answer="".join(answer_parts) if success else "",
            answer_source=AnswerSource.rag.value if success else AnswerSource.none.value,
            rag_trace_id=trace_id,
            terminal_reason_code=terminal_reason,
            citations=[to_citation_view(c) for c in citations_raw] if success else [],
            error_code=error_code,
        )
        await self._persist_turn(
            chat_session=chat_session,
            user=user,
            question=question,
            normalized=normalized,
            question_hash=question_hash_value,
            scopes=scopes,
            turn_id=turn_id,
            outcome=outcome,
            started_at=started_at,
        )

        if outcome.success:
            yield _final_sse(request_id, turn_id, outcome)
            # RAG final 已发出 → BackgroundTasks 补取真实 Token（不阻塞 final）
            if outcome.rag_trace_id:
                background_tasks.add_task(
                    _backfill_trace_tokens,
                    turn_id=turn_id,
                    trace_id=outcome.rag_trace_id,
                    service_user=rag_service_user,
                    trace_client=self.trace_client,
                )
        else:
            yield _error_sse(request_id, turn_id, outcome.error_code or ERR_RAG_BAD_RESPONSE)

    async def _fallback_to_status(
        self, session_id: str, rag_service_user: str
    ) -> dict[str, Any] | None:
        """SSE 异常断开后的有限兜底：返回终态事实 dict 或 None（表示无法确认）。

        - status completed 且完整（answer + trace_id）→ 以终态收口；
        - status failed / 无法确认终态 → None（绝不伪成功）。
        """
        try:
            status = await self.query_client.get_status(session_id, service_user=rag_service_user)
        except RagError:
            return None
        if status is None:
            return None
        if status.status == "completed":
            trace_id = str(status.trace_id or "").strip()
            if trace_id and status.answer:
                return {
                    "answer": status.answer,
                    "trace_id": trace_id or None,
                    "citations": list(status.citations),
                    "terminal_reason_code": status.terminal_reason_code,
                }
            return None
        # failed / pending / processing：无法确认终态
        return None

    # ---------- 持久化 ----------

    async def _persist_turn(
        self,
        *,
        chat_session: ChatSession,
        user: User,
        question: str,
        normalized: str,
        question_hash: str,
        scopes: list[str],
        turn_id: str,
        outcome: TurnOutcome,
        started_at: float,
    ) -> None:
        """终态一次性落库：user + assistant 共用 turn_id，seq_no 严格递增。

        锁 session 行后分配 seq_no（配合 UNIQUE(session_id, seq_no) 防持久化竞争）。
        """
        session = self.sessions.session
        now = utc_now_naive()
        # 锁会话行
        await session.execute(
            __import__("sqlalchemy", fromlist=["select"])
            .select(ChatSession)
            .where(ChatSession.id == chat_session.id)
            .with_for_update()
        )
        base_seq = await self.messages.max_seq_no(chat_session.id)
        user_seq = base_seq + 1
        assistant_seq = base_seq + 2

        user_message = ChatMessage(
            session_id=chat_session.id,
            turn_id=turn_id,
            seq_no=user_seq,
            role=MessageRole.user.value,
            content=question,
            status=MessageStatus.completed.value,
            answer_source=None,
            rag_trace_id=None,
            terminal_reason_code=None,
            citations_json=[],
            error_code=None,
            created_at=now,
            completed_at=now,
        )
        assistant_message = ChatMessage(
            session_id=chat_session.id,
            turn_id=turn_id,
            seq_no=assistant_seq,
            role=MessageRole.assistant.value,
            content=outcome.answer,
            status=(
                MessageStatus.completed.value if outcome.success else MessageStatus.failed.value
            ),
            answer_source=outcome.answer_source,
            rag_trace_id=outcome.rag_trace_id,
            terminal_reason_code=outcome.terminal_reason_code,
            citations_json=[c.model_dump(mode="json") for c in outcome.citations],
            error_code=outcome.error_code,
            created_at=now,
            completed_at=now,
        )
        await self.messages.append_messages([user_message, assistant_message])

        citation_count = len(outcome.citations)
        document_ids = extract_citation_document_ids(outcome.citations)
        latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        await self.logs.create_log(
            turn_id=turn_id,
            session_id=chat_session.id,
            channel="internal_web",
            user_id=user.id,
            external_subject_hash=None,
            question=question,
            normalized_question=normalized,
            normalized_question_hash=question_hash,
            allowed_scopes_json=scopes,
            answer_source=outcome.answer_source,
            faq_id=outcome.faq_id,
            rag_trace_id=outcome.rag_trace_id,
            terminal_reason_code=outcome.terminal_reason_code,
            citation_count=citation_count,
            citation_document_ids_json=document_ids,
            latency_ms=latency_ms,
            status="succeeded" if outcome.success else "failed",
            error_code=outcome.error_code,
            created_at=now,
        )

        # 第一次真正问答后生成默认标题（不调用 LLM）
        if chat_session.title == DEFAULT_SESSION_TITLE:
            chat_session.title = auto_title(normalized)
        chat_session.last_message_at = now
        chat_session.updated_at = now
        await session.commit()


def _progress(request_id: str, turn_id: str, stage: str, message: str) -> str:
    return _sse(
        EV_PROGRESS,
        {"request_id": request_id, "turn_id": turn_id, "stage": stage, "message": message},
    )


def _final_sse(request_id: str, turn_id: str, outcome: TurnOutcome) -> str:
    return _sse(
        EV_FINAL,
        {
            "request_id": request_id,
            "turn_id": turn_id,
            "answer": outcome.answer,
            "answer_source": outcome.answer_source,
            "trace_id": outcome.rag_trace_id,
            "citations": [c.model_dump(mode="json") for c in outcome.citations],
            "terminal_reason_code": outcome.terminal_reason_code,
        },
    )


async def _backfill_trace_tokens(
    turn_id: str,
    trace_id: str,
    service_user: str,
    trace_client: RagTraceClient | None,
) -> None:
    """BackgroundTasks：RAG final 后补取真实 Token。

    - FAQ 不运行本任务（无 RAG trace）；
    - 获取失败：问答仍成功，Token 保持 null，写安全 warning 日志；
    - available=false：Token 保持 null（不把“缺失”冒充真实 0）；
    - available=true：接受真实非负整数，包括 0。
    """
    client = trace_client
    if client is None:
        from app.rag.rag_trace_client import get_rag_trace_client

        client = get_rag_trace_client()
    try:
        snapshot = await client.get_token_usage(trace_id, service_user=service_user)
    except Exception:  # noqa: BLE001
        logger.warning("Trace Token 补取失败 trace_id=%s", trace_id, exc_info=True)
        return
    if snapshot is None or not snapshot.available:
        return
    if None in (snapshot.input_tokens, snapshot.output_tokens, snapshot.total_tokens):
        return
    try:
        from app.core.database import get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            repo = QaAccessLogRepository(db)
            await repo.update_tokens(
                turn_id=turn_id,
                input_tokens=snapshot.input_tokens,
                output_tokens=snapshot.output_tokens,
                total_tokens=snapshot.total_tokens,
            )
            await db.commit()
        logger.info(
            "Trace Token 回填成功 turn_id=%s tokens=%s/%s/%s",
            turn_id,
            snapshot.input_tokens,
            snapshot.output_tokens,
            snapshot.total_tokens,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Trace Token 回填写库失败 turn_id=%s", turn_id, exc_info=True)
