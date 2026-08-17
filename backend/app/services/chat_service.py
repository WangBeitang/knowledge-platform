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
from enum import StrEnum
from typing import Any

from fastapi import BackgroundTasks

from app.core.enums import AnswerSource, MessageRole, MessageStatus, SessionStatus
from app.core.errors import conflict, not_found
from app.core.time import utc_now_naive
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.rag.rag_errors import RagError, rag_bad_response
from app.rag.rag_query_client import RagQueryClient
from app.rag.rag_trace_client import RagTraceClient
from app.rag.scope_policy import dataset_ids_for_role, service_user_for_role
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


class TurnPhase(StrEnum):
    """一次问答在上游生命周期中的阶段（决策：下游交付 ≠ 上游 terminal）。

    - PRE_SUBMIT：尚未开始调用原 RAG POST /query（FAQ lookup 中/提交前）；
    - SUBMITTING：POST 已开始，但平台尚未确认响应，上游是否接受存在不确定性；
    - SUBMITTED：已明确确认原 RAG 接受（收到 200 + session_id 一致）；
    - RELEASE_SAFE：已确认上游 Query 不再运行（可靠上游 final/error，
      /status=completed|failed，或 404 确认无任务，或从未触达上游）。
    """

    PRE_SUBMIT = "pre_submit"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RELEASE_SAFE = "release_safe"


@dataclass
class TurnRecord:
    """一次进行中问答的必要小状态（registry 只保存这些，release-safe 后及时清理）。

    - phase：pre_submit → submitting → submitted → release_safe；
    - rag_service_user：进入 submitting 时提前保存，孤儿/歧义状态判定 /status 时复用；
    - recovering：是否正在被某个 recover owner 对账（防并发恢复竞态）；
    - persisted：本 Turn 终态是否已落库（避免 live error 已落 failed 后，
      recovery 再次以同一 turn_id 重复写 qa_access_logs）。
    """

    session_id: str
    user_id: str
    turn_id: str
    question: str
    normalized: str
    question_hash: str
    scopes: list[str]
    started_at: float
    phase: TurnPhase = TurnPhase.PRE_SUBMIT
    rag_service_user: str | None = None
    recovering: bool = False
    persisted: bool = False
    alive: bool = False  # 本 Turn 的流式 generator 是否仍在活跃执行


class ActiveTurnRegistry:
    """进程内按 session_id 的活跃 Turn 注册表（单机单 API worker 假设）。

    并发/生命周期语义（Stage 4 二次复核决策一/二/三）：
    - 同 session 同一时间只能有一个进行中问答；只有“能证明旧上游 Query 已不再运行”
      （release-safe）才允许释放占位并提交下一 Query；
    - pre_submit 断开：从未触达上游 → 可安全释放，下一问不永久 409；
    - submitting 网络断开：上游接受与否不确定 → 保留 acceptance-ambiguous 状态，
      下一问先 /status 对账（pending/processing→409；terminal→收口后释放；
      无法确认→保守 409）；
    - 所有关键状态更新都校验 turn_id，禁止旧请求删除/修改后来新建的 Turn；
    - recover 同一时刻最多一个 owner（recovering 标志），防止重复补齐同一 Turn；
    - 不引入 Redis 锁 / 分布式锁 / busy 数据库字段。
    """

    def __init__(self) -> None:
        self._records: dict[str, TurnRecord] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    async def get(self, session_id: str) -> TurnRecord | None:
        async with self._guard:
            return self._records.get(session_id)

    async def try_acquire(self, session_id: str, record: TurnRecord) -> bool:
        lock = await self._session_lock(session_id)
        async with lock:
            async with self._guard:
                if session_id in self._records:
                    return False
                self._records[session_id] = record
                return True

    async def begin_submitting(self, session_id: str, turn_id: str, rag_service_user: str) -> bool:
        """pre_submit → submitting（POST 开始前调用，提前保存 service 身份）。

        仅当当前记录仍是本 turn_id 且处于 pre_submit 才生效；否则返回 False。
        """
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is None or record.turn_id != turn_id or record.phase != TurnPhase.PRE_SUBMIT:
                return False
            record.phase = TurnPhase.SUBMITTING
            record.rag_service_user = rag_service_user
            return True

    async def mark_submitted(self, session_id: str, turn_id: str) -> bool:
        """submitting → submitted（POST 已确认接受）。"""
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is None or record.turn_id != turn_id:
                return False
            if record.phase == TurnPhase.SUBMITTING:
                record.phase = TurnPhase.SUBMITTED
            return True

    async def mark_persisted(self, session_id: str, turn_id: str) -> None:
        """记录本 Turn 终态已落库（防 recovery 重复写 qa_access_logs）。"""
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is not None and record.turn_id == turn_id:
                record.persisted = True

    async def mark_release_safe(self, session_id: str, turn_id: str) -> bool:
        """标记 release-safe（已确认上游不再运行或从未触达上游）。"""
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is None or record.turn_id != turn_id:
                return False
            record.phase = TurnPhase.RELEASE_SAFE
            return True

    async def begin_recover(self, session_id: str, turn_id: str) -> bool:
        """抢占 recover owner：同一时刻最多一个；返回 False 表示被他人持有/状态不符。"""
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if (
                record is None
                or record.turn_id != turn_id
                or record.recovering
                or record.phase == TurnPhase.RELEASE_SAFE
            ):
                return False
            record.recovering = True
            return True

    async def finish_recover(self, session_id: str, turn_id: str, *, release: bool) -> bool:
        """recover 结束：release=True 释放占位；否则清除 recovering 标记。

        两者都校验 turn_id，防止 stale recover 释放后来新建的 Turn。
        """
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is None or record.turn_id != turn_id:
                return False
            if release:
                self._records.pop(session_id, None)
            else:
                record.recovering = False
            return True

    async def release(self, session_id: str, turn_id: str) -> bool:
        """按 turn_id 校验后释放；turn_id 不匹配（stale 请求）→ 不删除新 Turn。"""
        lock = await self._session_lock(session_id)
        async with lock:
            record = self._records.get(session_id)
            if record is None or record.turn_id != turn_id:
                return False
            self._records.pop(session_id, None)
            return True

    def active_count(self) -> int:
        return len(self._records)


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


@dataclass
class StatusFallback:
    """/status 兜底判定结果（区分“可收口”与“仅可报错”）。"""

    kind: str  # "terminal"（final 收口）| "failed"（error+release-safe）| "unknown"（error+保留）

    answer: str = ""
    trace_id: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    terminal_reason_code: str | None = None


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

    def build_turn_record(
        self,
        *,
        user: User,
        session_id: str,
        question: str,
        normalized: str,
        question_hash_value: str,
        scopes: list[str],
        started_at: float,
    ) -> TurnRecord:
        """构造本轮问答的 registry 记录（turn_id 在此统一生成）。"""
        return TurnRecord(
            session_id=session_id,
            user_id=user.id,
            turn_id=str(uuid.uuid4()),
            question=question,
            normalized=normalized,
            question_hash=question_hash_value,
            scopes=scopes,
            started_at=started_at,
        )

    async def try_acquire_turn(self, session_id: str, record: TurnRecord) -> bool:
        return await _active_turns.try_acquire(session_id, record)

    async def maybe_release_turn(self, session_id: str, turn_id: str) -> None:
        """路由 finally：仅 release-safe（已确认上游不再运行）才释放。

        客户端断开/异常 → 不满足 → 保留 orphaned 供下次触达判定；
        校验 turn_id，禁止 stale finally 删除后来新建的 Turn。
        """
        record = await _active_turns.get(session_id)
        if (
            record is not None
            and record.turn_id == turn_id
            and record.phase == TurnPhase.RELEASE_SAFE
        ):
            await _active_turns.release(session_id, turn_id)

    async def recover_orphaned(
        self,
        session_id: str,
        user_id: str,
        *,
        external_subject_hash: str | None = None,
    ) -> None:
        """上一轮断开/歧义但未确认上游 terminal：先查上游 /status 判定。

        - 无记录 / release-safe / pre_submit（从未触达上游）→ 安全释放，允许新 Query；
        - /status=404（上游确认无该 session 任务）→ 旧 Query 已不在运行 → 释放；
        - /status=pending|processing → 409（旧 Query 仍在跑，禁止重叠）；
        - /status=completed（trace_id+answer 完整）→ 补齐旧 Turn 终态后释放；
          completed 但字段不完整：已落库则释放，否则保守 409（不静默释放）；
        - /status=failed → 补齐失败态后释放；
        - /status 网络错误/无法确认 → 保守 409，不提交新 /query。

        外部 API 调用时 user_id=None、external_subject_hash=加盐哈希：
        TurnRecord.user_id 为空 → recover 持久化走 external_api 渠道语义。

        并发安全：begin_recover 抢占唯一 owner；被抢占者直接 409；
        所有关键更新（finish_recover/release）校验 turn_id。
        """
        record = await _active_turns.get(session_id)
        if record is None:
            return
        if record.alive:
            # 上一轮 Turn 仍在活跃执行（并发触达同一 session）：不触碰，保守 409
            raise conflict("该会话正在回答中，请稍后再试")
        if record.phase == TurnPhase.RELEASE_SAFE:
            await _active_turns.release(session_id, record.turn_id)
            return
        if record.phase == TurnPhase.PRE_SUBMIT:
            # 从未触达上游：没有上游 Query，安全释放，下一问可继续
            await _active_turns.release(session_id, record.turn_id)
            return
        # SUBMITTING / SUBMITTED：需要与上游对账（单 owner 防并发恢复竞态）
        if not await _active_turns.begin_recover(session_id, record.turn_id):
            raise conflict("上一轮问答正在确认中，请稍后再试")
        try:
            try:
                status = await self.query_client.get_status(
                    session_id, service_user=record.rag_service_user or ""
                )
            except RagError:
                # 无法确认 → 保守 409；清除 recovering 供下次重试
                await _active_turns.finish_recover(session_id, record.turn_id, release=False)
                raise conflict("上一轮问答状态无法确认，请稍后重试") from None
            if status is None:
                # /status=404：上游确认无该 session 任务 → 旧 Query 已不在运行；
                # 未落库时补齐失败态（本轮问答未能完成），随后释放允许下一问
                if not record.persisted:
                    await self._recover_failed(record, external_subject_hash=external_subject_hash)
                await _active_turns.finish_recover(session_id, record.turn_id, release=True)
                return
            if status.status in ("pending", "processing"):
                await _active_turns.finish_recover(session_id, record.turn_id, release=False)
                raise conflict("该会话仍在处理上一轮问答，请稍后再试")
            if status.status == "completed":
                if status.trace_id and status.answer:
                    if not record.persisted:
                        await self._recover_terminal(
                            record, status, external_subject_hash=external_subject_hash
                        )
                    await _active_turns.finish_recover(session_id, record.turn_id, release=True)
                elif record.persisted:
                    # 已落库（live error 已交付）→ 无需补齐，释放
                    await _active_turns.finish_recover(session_id, record.turn_id, release=True)
                else:
                    # completed 但终态字段不完整且未落库：无法安全补齐 → 保守 409
                    await _active_turns.finish_recover(session_id, record.turn_id, release=False)
                    raise conflict("上一轮问答结果不完整，请稍后重试")
                return
            if status.status == "failed":
                if not record.persisted:
                    await self._recover_failed(record, external_subject_hash=external_subject_hash)
                await _active_turns.finish_recover(session_id, record.turn_id, release=True)
                return
            # 未知状态已由 Adapter 契约校验拒绝（RAG_BAD_RESPONSE），走 except
            await _active_turns.finish_recover(session_id, record.turn_id, release=False)
            raise conflict("上一轮问答状态无法确认，请稍后重试")
        except Exception:  # noqa: BLE001
            # 未预期异常：确保不残留 recovering 锁死后续触达（conflict 路径已在
            # 抛出前 finish_recover(release=False)，此处只兜底未预期异常）
            await _active_turns.finish_recover(session_id, record.turn_id, release=False)
            raise

    async def _recover_terminal(
        self, record: TurnRecord, status, *, external_subject_hash: str | None = None
    ) -> None:
        """尽力补齐断开 Turn 的终态持久化（上游已 completed 且完整）。

        record 由归属校验通过的 Turn 创建（内部 get_owned / 外部 external 会话映射），
        因此 recover 时按 id 定位会话即可，不重复校验归属。
        """
        chat_session = await self.sessions.get_by_id(record.session_id)
        if chat_session is None:
            return
        outcome = TurnOutcome(
            success=True,
            answer=status.answer,
            answer_source=AnswerSource.rag.value,
            rag_trace_id=status.trace_id or None,
            terminal_reason_code=status.terminal_reason_code,
            citations=[to_citation_view(c) for c in status.citations],
        )
        await self._persist_turn(
            chat_session=chat_session,
            user_id=record.user_id,
            question=record.question,
            normalized=record.normalized,
            question_hash=record.question_hash,
            scopes=record.scopes,
            turn_id=record.turn_id,
            outcome=outcome,
            started_at=record.started_at,
            channel=("external_api" if record.user_id is None else "internal_web"),
            external_subject_hash=external_subject_hash,
        )
        logger.info("断开 Turn 终态补齐 turn_id=%s", record.turn_id)

    async def _recover_failed(
        self, record: TurnRecord, *, external_subject_hash: str | None = None
    ) -> None:
        """尽力补齐断开 Turn 的失败态（上游 status=failed，非客户端断开导致）。"""
        chat_session = await self.sessions.get_by_id(record.session_id)
        if chat_session is None:
            return
        outcome = TurnOutcome(success=False, error_code=ERR_RAG_BAD_RESPONSE)
        await self._persist_turn(
            chat_session=chat_session,
            user_id=record.user_id,
            question=record.question,
            normalized=record.normalized,
            question_hash=record.question_hash,
            scopes=record.scopes,
            turn_id=record.turn_id,
            outcome=outcome,
            started_at=record.started_at,
            channel=("external_api" if record.user_id is None else "internal_web"),
            external_subject_hash=external_subject_hash,
        )
        logger.info("断开 Turn 失败态补齐 turn_id=%s", record.turn_id)

    async def get_or_create_external_session(
        self, *, external_session_id: str, external_subject_hash: str
    ) -> ChatSession:
        """外部会话轻量映射（数据对象 §4.6）：按外部会话 ID + 用户哈希定位平台会话。

        - 平台会话 ID（UUID）即上游 RAG session_id，与内部员工会话天然隔离；
        - 同一个外部会话的多轮问题复用同一平台会话（保持上游多轮上下文）；
        - 并发首问极小概率创建两个会话：后续请求总会命中最近创建的一个。
        """
        chat_session = await self.sessions.find_external_session(
            external_session_id, external_subject_hash
        )
        if chat_session is not None:
            return chat_session
        chat_session = await self.sessions.create_external_session(
            external_session_id=external_session_id,
            external_subject_hash=external_subject_hash,
            title=DEFAULT_SESSION_TITLE,
            status=SessionStatus.active.value,
            created_at=utc_now_naive(),
        )
        await self.sessions.session.commit()
        return chat_session

    def build_external_turn_record(
        self,
        *,
        session_id: str,
        question: str,
        normalized: str,
        question_hash_value: str,
        scopes: list[str],
        started_at: float,
    ) -> TurnRecord:
        """构造外部问答的 registry 记录：user_id 恒为空（匿名，不绑定员工账号）。"""
        return TurnRecord(
            session_id=session_id,
            user_id=None,
            turn_id=str(uuid.uuid4()),
            question=question,
            normalized=normalized,
            question_hash=question_hash_value,
            scopes=scopes,
            started_at=started_at,
        )

    async def stream_external(
        self,
        *,
        external_session_id: str,
        external_subject_hash: str,
        chat_session: ChatSession,
        question: str,
        normalized: str,
        request_id: str,
        background_tasks: BackgroundTasks,
        started_at: float,
        record: TurnRecord,
    ) -> AsyncIterator[str]:
        """外部知识 API 流式问答（《API 接口设计》§10 / §11，与内部共用 SSE 契约）。

        外部权限服务端固定（禁止客户端输入扩大）：
        - channel = external_api；
        - allowed_scopes = [external_public]（scope_policy "external" 角色矩阵）；
        - rag_dataset_ids = [RAG_EXTERNAL_DATASET_ID]；
        - rag_service_user = RAG_SERVICE_USER_EXTERNAL；
        - user_id = None；external_user_id 仅以加盐哈希关联日志/UV，不参与权限计算。

        FAQ 精确命中：只允许 external_public，answer_source=faq_cache、
        trace_id=null、citations=[]，不调用 RAG、不伪造 Citation；
        未命中：走 `_rag_flow`（内部问答同一条编排，不复制第二套 RAG 流程）。
        """
        turn_id = record.turn_id
        session_id = chat_session.id
        question_hash_value = record.question_hash
        scopes = record.scopes

        # ready
        yield _sse(
            EV_READY,
            {"request_id": request_id, "turn_id": turn_id, "session_id": session_id},
        )
        # progress(faq_lookup)
        yield _progress(request_id, turn_id, STAGE_FAQ_LOOKUP, "正在检索常见问题")

        # FAQ 精确短路（只允许 external_public）
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
                user_id=None,
                question=question,
                normalized=normalized,
                question_hash=question_hash_value,
                scopes=scopes,
                turn_id=turn_id,
                outcome=outcome,
                started_at=started_at,
                channel="external_api",
                external_subject_hash=external_subject_hash,
            )
            # FAQ 命中禁止调用原 RAG；交付成功后显式记录命中
            try:
                await self.faq_service.record_hit(faq_hit.faq_id)
            except Exception:  # noqa: BLE001 命中计数失败不影响已交付答案
                logger.warning("FAQ hit_count 自增失败 faq_id=%s", faq_hit.faq_id)
            await _active_turns.mark_persisted(session_id, turn_id)
            # FAQ 从未触达上游 → release-safe
            await _active_turns.mark_release_safe(session_id, turn_id)
            yield _final_sse(request_id, turn_id, outcome)
            return

        # FAQ 未命中 → 原 RAG：固定 external 范围/Dataset/服务身份
        rag_service_user = service_user_for_role("external")
        dataset_ids = dataset_ids_for_role("external")
        async for chunk in self._rag_flow(
            request_id=request_id,
            turn_id=turn_id,
            session_id=session_id,
            chat_session=chat_session,
            user_id=None,
            question=question,
            normalized=normalized,
            question_hash=question_hash_value,
            scopes=scopes,
            rag_service_user=rag_service_user,
            dataset_ids=dataset_ids,
            started_at=started_at,
            background_tasks=background_tasks,
            channel="external_api",
            external_subject_hash=external_subject_hash,
        ):
            yield chunk

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
        record: TurnRecord,
    ) -> AsyncIterator[str]:
        """平台 SSE 事件流（调用方负责 acquire / orphaned 保留与终态标记）。

        - 可靠确认上游不再运行（上游 final/error、/status=completed/failed、
          404 无任务，或从未触达上游）→ 内部 mark_release_safe，route finally 释放；
        - 客户端断开 / 网络断流 / 下游 error 但上游仍在运行 → 不标记，
          Turn 保留为 orphaned/submitting，下次触达先 /status 对账（决策一/二）。
        """
        turn_id = record.turn_id
        session_id = chat_session.id
        question_hash_value = record.question_hash
        scopes = record.scopes

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
                user_id=user.id,
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
            await _active_turns.mark_persisted(session_id, turn_id)
            # FAQ 从未触达上游 → release-safe
            await _active_turns.mark_release_safe(session_id, turn_id)
            yield _final_sse(request_id, turn_id, outcome)
            return

        # FAQ 未命中 → 原 RAG：固定按当前 DB 角色计算范围/Dataset/服务身份
        rag_service_user = service_user_for_role(user.role)
        dataset_ids = dataset_ids_for_role(user.role)
        async for chunk in self._rag_flow(
            request_id=request_id,
            turn_id=turn_id,
            session_id=session_id,
            chat_session=chat_session,
            user_id=user.id,
            question=question,
            normalized=normalized,
            question_hash=question_hash_value,
            scopes=scopes,
            rag_service_user=rag_service_user,
            dataset_ids=dataset_ids,
            started_at=started_at,
            background_tasks=background_tasks,
            channel="internal_web",
            external_subject_hash=None,
        ):
            yield chunk

    async def _rag_flow(
        self,
        *,
        request_id: str,
        turn_id: str,
        session_id: str,
        chat_session: ChatSession,
        user_id: str,
        question: str,
        normalized: str,
        question_hash: str,
        scopes: list[str],
        rag_service_user: str,
        dataset_ids: list[str],
        started_at: float,
        background_tasks: BackgroundTasks,
        channel: str,
        external_subject_hash: str | None,
    ) -> AsyncIterator[str]:
        """RAG 路径的统一编排（内部问答与外部 API 共用，禁止复制第二套）。

        - submit → 流式 → 断流 /status 兜底 → 终态唯一交付；
        - 上游身份与 Dataset 由调用方按冻结矩阵传入（内部按 DB 角色，
          外部固定 RAG_SERVICE_USER_EXTERNAL + RAG_EXTERNAL_DATASET_ID）；
        - 持久化差异（channel / external_subject_hash）由调用方传入，
          内部 channel=internal_web、外部 channel=external_api；
        - release-safe 判定与 Stage 4 冻结语义完全一致（可靠上游终态
          / /status terminal / 404 / 从未触达上游才允许释放）。
        """
        yield _progress(request_id, turn_id, STAGE_RAG_SUBMIT, "正在提交知识检索")
        # POST 开始前进入 submitting 并提前保存 service 身份：若提交发生网络
        # timeout/connection，上游是否已接受存在不确定性（acceptance-ambiguous），
        # 平台必须保留该状态，由下一次触达先 /status 对账。
        await _active_turns.begin_submitting(session_id, turn_id, rag_service_user)
        try:
            await self.query_client.submit_query(
                query=question,
                session_id=session_id,
                dataset_ids=dataset_ids,
                service_user=rag_service_user,
            )
            # 已明确确认上游接受（200 + session_id 一致）
            await _active_turns.mark_submitted(session_id, turn_id)
        except RagError as exc:
            outcome = TurnOutcome(success=False, error_code=_safe_rag_error_code(exc))
            await self._persist_turn(
                chat_session=chat_session,
                user_id=user_id,
                question=question,
                normalized=normalized,
                question_hash=question_hash,
                scopes=scopes,
                turn_id=turn_id,
                outcome=outcome,
                started_at=started_at,
                channel=channel,
                external_subject_hash=external_subject_hash,
            )
            await _active_turns.mark_persisted(session_id, turn_id)
            if getattr(exc, "acceptance_ambiguous", False):
                # 网络超时/断链/网关超时：上游是否接受不确定 → 不 release-safe，
                # 保留 acceptance-ambiguous 状态；下一问先 /status 对账
                logger.info("submit 接受性歧义 turn_id=%s code=%s", turn_id, outcome.error_code)
            else:
                # 上游明确拒绝（非 2xx / 契约错误）：未产生上游任务 → release-safe
                await _active_turns.mark_release_safe(session_id, turn_id)
            yield _error_sse(request_id, turn_id, outcome.error_code)
            return

        yield _progress(request_id, turn_id, STAGE_RAG_PROGRESS, "正在检索知识库并生成答案")

        answer_parts: list[str] = []
        citations_raw: list[dict[str, Any]] = []
        trace_id: str | None = None
        terminal_reason: str | None = None
        error_code: str | None = None
        stream_error_code: str | None = None
        saw_terminal = False
        # 上游 terminal 是否已被“可靠”确认（上游 final/error、/status=completed/failed、
        # 或 404 确认无任务）。只有 release-safe 才允许释放 registry 提交下一 Query；
        # 仅“下游已向浏览器交付 error”不算（决策一）。
        upstream_release_safe = False

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
                    answer_text = data.get("answer")
                    if not isinstance(answer_text, str):
                        raise rag_bad_response("上游 final 缺少 answer")
                    upstream_trace_id = str(data.get("trace_id") or "").strip()
                    if not upstream_trace_id:
                        raise rag_bad_response("上游 final 缺少 trace_id")
                    citations_payload = data.get("citations")
                    if not isinstance(citations_payload, list) or not all(
                        isinstance(c, dict) for c in citations_payload
                    ):
                        raise rag_bad_response("上游 final citations 结构异常")
                    terminal_raw = data.get("terminal_reason_code")
                    if terminal_raw is not None and not isinstance(terminal_raw, str):
                        raise rag_bad_response("上游 final terminal_reason_code 类型异常")
                    answer_parts = [answer_text] if answer_text else answer_parts
                    trace_id = upstream_trace_id or None
                    citations_raw = citations_payload
                    terminal_reason = terminal_raw
                    saw_terminal = True
                    upstream_release_safe = True  # 上游自身宣告 final = 可靠 terminal
                    break
                elif event == EV_ERROR:
                    # 上游显式 error：不直接透传上游异常字符串；上游自身宣告终态
                    error_code = ERR_RAG_BAD_RESPONSE
                    saw_terminal = True
                    upstream_release_safe = True
                    break
                else:
                    continue  # 未知事件：不破坏流
        except RagError as exc:
            # 网络断流（timeout/connection）或契约异常：不直接定为失败，
            # 先有限 GET /status 兜底（任务书决策 3）；是否 release-safe 由 /status 决定
            stream_error_code = _safe_rag_error_code(exc)
            saw_terminal = False
            upstream_release_safe = False
        except Exception:  # noqa: BLE001 未预期异常：连接不截断，走稳定错误
            logger.exception("RAG 流式处理异常 session=%s", session_id)
            stream_error_code = ERR_RAG_BAD_RESPONSE
            saw_terminal = False
            upstream_release_safe = False

        # 未观察到明确 final/error（自然 EOF / 网络断流 / 契约异常）→ 有限兜底查 /status
        if not saw_terminal and error_code is None:
            yield _progress(request_id, turn_id, STAGE_FINALIZING, "正在整理结果")
            fallback = await self._fallback_to_status(session_id, rag_service_user)
            if fallback is None:
                error_code = stream_error_code or ERR_RAG_BAD_RESPONSE
                upstream_release_safe = False
            elif fallback.kind == "terminal":
                answer_parts = [fallback.answer] if fallback.answer else answer_parts
                trace_id = fallback.trace_id
                citations_raw = fallback.citations
                terminal_reason = fallback.terminal_reason_code
                saw_terminal = True
                upstream_release_safe = True
            else:
                # failed / absent（404 确认无任务）→ error + release-safe；
                # unknown（pending/processing/无法确认）→ error + 保留占位
                error_code = stream_error_code or ERR_RAG_BAD_RESPONSE
                upstream_release_safe = fallback.kind != "unknown"

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
            user_id=user_id,
            question=question,
            normalized=normalized,
            question_hash=question_hash,
            scopes=scopes,
            turn_id=turn_id,
            outcome=outcome,
            started_at=started_at,
            channel=channel,
            external_subject_hash=external_subject_hash,
        )
        await _active_turns.mark_persisted(session_id, turn_id)
        if upstream_release_safe:
            await _active_turns.mark_release_safe(session_id, turn_id)

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
    ) -> StatusFallback | None:
        """SSE 异常断开后的有限兜底：基于 /status 判定终态事实。

        返回：
        - StatusFallback(kind="terminal", ...) → 可 final 收口（completed 且完整）；
        - StatusFallback(kind="failed") → 上游已 failed，error + release-safe；
        - StatusFallback(kind="absent") → /status=404，上游确认无任务，error + release-safe；
        - StatusFallback(kind="unknown") → pending/processing/completed 不完整/无法确认，
          error + 保留占位（下一问先 /status 对账）；
        - None → /status 调用失败（网络错误/契约错误），无法确认。
        """
        try:
            status = await self.query_client.get_status(session_id, service_user=rag_service_user)
        except RagError:
            return None
        if status is None:
            # 404：上游确认无该 session 任务 → 旧 Query 已不在运行（无内容可收口）
            return StatusFallback(kind="absent")
        if status.status == "completed":
            trace_id = str(status.trace_id or "").strip()
            if trace_id and status.answer:
                return StatusFallback(
                    kind="terminal",
                    answer=status.answer,
                    trace_id=trace_id or None,
                    citations=list(status.citations),
                    terminal_reason_code=status.terminal_reason_code,
                )
            # completed 但终态字段不完整：无法安全收口 → 保守 unknown
            return StatusFallback(kind="unknown")
        if status.status == "failed":
            return StatusFallback(kind="failed")
        # pending / processing：上游仍在运行
        return StatusFallback(kind="unknown")

    # ---------- 持久化 ----------

    async def _persist_turn(
        self,
        *,
        chat_session: ChatSession,
        user_id: str,
        question: str,
        normalized: str,
        question_hash: str,
        scopes: list[str],
        turn_id: str,
        outcome: TurnOutcome,
        started_at: float,
        channel: str = "internal_web",
        external_subject_hash: str | None = None,
    ) -> None:
        """终态一次性落库：user + assistant 共用 turn_id，seq_no 严格递增。

        锁 session 行后分配 seq_no（配合 UNIQUE(session_id, seq_no) 防持久化竞争）。
        内部问答（默认）channel=internal_web、user_id 为员工；外部 API 由
        `stream_external` 传入 channel=external_api、user_id=None、
        external_subject_hash=加盐哈希（冻结 API §10 / 数据对象 §4.8）。
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
            channel=channel,
            user_id=user_id,
            external_subject_hash=external_subject_hash,
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
