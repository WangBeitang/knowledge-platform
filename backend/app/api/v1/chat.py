"""内部会话与问答路由（《API 接口设计》§9 / §11）。

权限边界：
- 所有接口继续使用现有 JWT 鉴权；会话只能操作自己的（他人 session 统一 404，
  不泄漏其是否存在）；
- 管理员也不能查看其他员工的聊天内容；
- `messages:stream` 请求体只有 question（extra="forbid"，多余字段直接 422）；
- 前端和请求体都不允许指定 scope / dataset（服务端按当前 DB 角色计算）。
"""

import time

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import bad_request, conflict
from app.core.normalizer import normalize_question
from app.core.request_context import get_request_id
from app.core.time import iso8601
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.rag.rag_query_client import get_rag_query_client
from app.rag.rag_trace_client import get_rag_trace_client
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.chat_session_repository import ChatSessionRepository
from app.repositories.faq_repository import FaqRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.chat import (
    EMPTY_QUESTION_CODE,
    ChatMessageResponse,
    ChatMessageStreamRequest,
    ChatMessageView,
    ChatSessionCreateRequest,
    ChatSessionDetailResponse,
    ChatSessionDetailView,
    ChatSessionListResponse,
    ChatSessionUpdateRequest,
    ChatSessionView,
    ChatSessionViewResponse,
    CitationView,
)
from app.services.audit_service import AuditService
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


def _chat_service(session: AsyncSession) -> ChatService:
    return ChatService(
        sessions=ChatSessionRepository(session),
        messages=ChatMessageRepository(session),
        logs=QaAccessLogRepository(session),
        faq_repository=FaqRepository(session),
        audit=AuditService(AuditLogRepository(session)),
        query_client=get_rag_query_client(),
        trace_client=get_rag_trace_client(),
    )


def _session_view(chat_session: ChatSession) -> ChatSessionView:
    return ChatSessionView(
        id=chat_session.id,
        channel=chat_session.channel,
        user_id=chat_session.user_id,
        title=chat_session.title,
        status=chat_session.status,
        last_message_at=iso8601(chat_session.last_message_at),
        created_at=iso8601(chat_session.created_at),
        updated_at=iso8601(chat_session.updated_at),
    )


def _message_view(message: ChatMessage) -> ChatMessageView:
    citations: list[CitationView] = []
    for raw in message.citations_json or []:
        if isinstance(raw, dict):
            citations.append(CitationView.model_validate(raw))
    return ChatMessageView(
        id=message.id,
        session_id=message.session_id,
        turn_id=message.turn_id,
        seq_no=message.seq_no,
        role=message.role,
        content=message.content,
        status=message.status,
        answer_source=message.answer_source,
        rag_trace_id=message.rag_trace_id,
        terminal_reason_code=message.terminal_reason_code,
        citations=citations,
        error_code=message.error_code,
        created_at=iso8601(message.created_at),
        completed_at=iso8601(message.completed_at),
    )


@router.post("/sessions", response_model=ChatSessionViewResponse, status_code=201)
async def create_session(
    payload: ChatSessionCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ChatSessionViewResponse:
    chat_session = await _chat_service(session).create_session(user)
    return ChatSessionViewResponse(request_id=get_request_id(), data=_session_view(chat_session))


@router.get("/sessions", response_model=ChatSessionListResponse)
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ChatSessionListResponse:
    items, total = await _chat_service(session).list_sessions(user, page=page, page_size=page_size)
    return ChatSessionListResponse(
        request_id=get_request_id(),
        data={
            "items": [_session_view(item) for item in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        },
    )


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailResponse)
async def get_session_detail(
    session_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ChatSessionDetailResponse:
    service = _chat_service(session)
    chat_session = await service.get_session_detail(user, session_id)
    messages = await service.messages.list_by_session(session_id)
    return ChatSessionDetailResponse(
        request_id=get_request_id(),
        data=ChatSessionDetailView(
            session=_session_view(chat_session),
            messages=[_message_view(message) for message in messages],
        ),
    )


@router.patch("/sessions/{session_id}", response_model=ChatSessionViewResponse)
async def update_session(
    session_id: str,
    payload: ChatSessionUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ChatSessionViewResponse:
    chat_session = await _chat_service(session).update_session(
        user,
        session_id,
        title=payload.title,
        status=payload.status,
        client_ip=_client_ip(request),
    )
    return ChatSessionViewResponse(request_id=get_request_id(), data=_session_view(chat_session))


@router.delete("/sessions/{session_id}", response_model=ChatMessageResponse)
async def delete_session(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ChatMessageResponse:
    await _chat_service(session).delete_session(user, session_id, client_ip=_client_ip(request))
    return ChatMessageResponse(
        request_id=get_request_id(),
        data={"id": session_id, "message": "会话已删除"},
    )


@router.post("/sessions/{session_id}/messages:stream")
async def stream_message(
    session_id: str,
    payload: ChatMessageStreamRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """内部流式问答（SSE）。前置校验在响应开始前完成，返回普通 JSON 错误。"""
    service = _chat_service(session)
    chat_session = await service.assert_can_stream(session_id, user.id)
    normalized = normalize_question(payload.question)
    if not normalized:
        raise bad_request("问题不能为空", code=EMPTY_QUESTION_CODE)
    # 后端并发校验：同 session 同一时间只能有一个进行中的问答（409）
    if not await service.try_acquire_turn(session_id):
        raise conflict("该会话正在回答中，请稍后再试")
    request_id = get_request_id()
    started_at = time.perf_counter()

    async def generate():
        try:
            async for chunk in service.stream_answer(
                user=user,
                chat_session=chat_session,
                question=payload.question,
                normalized=normalized,
                request_id=request_id,
                background_tasks=background_tasks,
                started_at=started_at,
            ):
                yield chunk
        finally:
            # 无论终态、异常还是客户端断开，都必须释放并发占位
            await service.release_turn(session_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or None
    return request.client.host[:64] if request.client else None
