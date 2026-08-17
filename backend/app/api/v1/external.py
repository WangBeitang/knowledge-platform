"""外部知识 API 路由（《API 接口设计》§10 / §11 / §16）。

契约：
- `POST /api/v1/external/knowledge/messages:stream`，请求头 `X-Service-Key`；
- 请求体只有 external_session_id / external_user_id / question（extra="forbid"，
  提交 role / knowledge_scope / allowed_scopes / dataset_id(s) / 服务身份等一律 422）；
- 外部权限服务端固定（external_public / RAG_EXTERNAL_DATASET_ID /
  RAG_SERVICE_USER_EXTERNAL），不得按客户端输入扩大；
- Service Key 失败 → 401 SERVICE_AUTH_FAILED（普通 JSON，非 SSE）；
- 外部响应不泄漏 Dataset ID / 服务身份 / Service Key / 内部连接信息 / 内部异常。

鉴权失败与 422 校验发生在流式响应开始前，以普通 JSON 返回。
"""

import time

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import AppError, bad_request, conflict
from app.core.normalizer import normalize_question, question_hash
from app.core.request_context import get_request_id
from app.core.security import external_subject_hash, verify_service_key
from app.rag.rag_query_client import get_rag_query_client
from app.rag.rag_trace_client import get_rag_trace_client
from app.rag.scope_policy import scopes_for_role
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.chat_session_repository import ChatSessionRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.external import EMPTY_QUESTION_CODE, ExternalMessageStreamRequest
from app.services.audit_service import AuditService
from app.services.chat_service import ChatService

router = APIRouter(prefix="/external", tags=["external"])

# 冻结错误码（API §3）：外部服务密钥错误
SERVICE_AUTH_FAILED = AppError("SERVICE_AUTH_FAILED", "外部服务密钥错误", status_code=401)


def require_service_key(
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> None:
    """外部 Service Key 鉴权依赖：缺失/错误一律 401 SERVICE_AUTH_FAILED。

    密钥仅来自 env（Settings.service_api_key），不写数据库、不写日志和审计、
    不返回给客户端。
    """
    if not verify_service_key(x_service_key):
        raise SERVICE_AUTH_FAILED


def _external_service(session: AsyncSession) -> ChatService:
    return ChatService(
        sessions=ChatSessionRepository(session),
        messages=ChatMessageRepository(session),
        logs=QaAccessLogRepository(session),
        faq_repository=FaqRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        query_client=get_rag_query_client(),
        trace_client=get_rag_trace_client(),
    )


@router.post("/knowledge/messages:stream")
async def stream_external_message(
    payload: ExternalMessageStreamRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
) -> StreamingResponse:
    """外部流式问答（SSE）。前置校验在响应开始前完成，返回普通 JSON 错误。"""
    service = _external_service(session)
    normalized = normalize_question(payload.question)
    if not normalized:
        raise bad_request("问题不能为空", code=EMPTY_QUESTION_CODE)
    external_hash = external_subject_hash(payload.external_user_id)
    # 外部权限服务端固定（channel=external_api / [external_public]）
    scopes = scopes_for_role("external")
    chat_session = await service.get_or_create_external_session(
        external_session_id=payload.external_session_id,
        external_subject_hash=external_hash,
    )
    # 上一轮断开但上游可能仍在运行：先 /status 对账（与内部同一 recover 语义）
    await service.recover_orphaned(chat_session.id, None, external_subject_hash=external_hash)
    # 同外部会话并发 Turn 防护（复用进程内 registry）
    started_at = time.perf_counter()
    record = service.build_external_turn_record(
        session_id=chat_session.id,
        question=payload.question,
        normalized=normalized,
        question_hash_value=question_hash(normalized),
        scopes=scopes,
        started_at=started_at,
    )
    if not await service.try_acquire_turn(chat_session.id, record):
        raise conflict("该会话正在回答中，请稍后再试")
    record.alive = True
    request_id = get_request_id()

    async def generate():
        try:
            async for chunk in service.stream_external(
                external_session_id=payload.external_session_id,
                external_subject_hash=external_hash,
                chat_session=chat_session,
                question=payload.question,
                normalized=normalized,
                request_id=request_id,
                background_tasks=background_tasks,
                started_at=started_at,
                record=record,
            ):
                yield chunk
        finally:
            # 与内部一致：断开后 Turn 变可对账 orphaned；仅 release-safe 才释放
            record.alive = False
            await service.maybe_release_turn(chat_session.id, record.turn_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
