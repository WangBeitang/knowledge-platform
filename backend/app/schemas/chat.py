"""内部会话与问答 DTO（《API 接口设计》§9 / §11、§5.3 CitationSnapshot）。

冻结约束：
- `messages:stream` 请求体只有 `question`，任何额外字段必须 422（extra="forbid"）；
- 前端和请求体都不允许指定 scope / dataset；
- Citation 映射规则见 Stage 4 决策（本地/Web 白名单投影），缺失即 null，不伪造。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiResponse, Page

CHAT_SCHEMA_CONFIG = ConfigDict(extra="forbid")

EMPTY_QUESTION_CODE = "EMPTY_QUESTION"


class ChatSessionCreateRequest(BaseModel):
    """新建会话：无业务字段（标题由服务端默认“新会话”）。"""

    model_config = CHAT_SCHEMA_CONFIG


class ChatSessionUpdateRequest(BaseModel):
    """修改标题或归档（PATCH 字段严格沿用冻结 API Schema）。"""

    model_config = CHAT_SCHEMA_CONFIG

    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class ChatMessageStreamRequest(BaseModel):
    """内部流式问答请求：只允许 question。"""

    model_config = CHAT_SCHEMA_CONFIG

    question: str = Field(min_length=1, max_length=4000)


class CitationView(BaseModel):
    """冻结 CitationSnapshot 的平台安全投影（Web 无本地 ID 是合法状态）。"""

    document_id: str | None = None
    chunk_id: str | None = None
    document_name: str | None = None
    content_preview: str | None = None
    score: float | None = None
    source_url: str | None = None
    index_version: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ChatSessionView(BaseModel):
    id: str
    channel: str
    user_id: str | None = None
    title: str
    status: str
    last_message_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ChatMessageView(BaseModel):
    id: str
    session_id: str
    turn_id: str
    seq_no: int
    role: str
    content: str
    status: str
    answer_source: str | None = None
    rag_trace_id: str | None = None
    terminal_reason_code: str | None = None
    citations: list[CitationView] = Field(default_factory=list)
    error_code: str | None = None
    created_at: str | None = None
    completed_at: str | None = None


class ChatSessionListData(Page[ChatSessionView]):
    pass


class ChatSessionListResponse(ApiResponse[ChatSessionListData]):
    pass


class ChatSessionDetailView(BaseModel):
    session: ChatSessionView
    messages: list[ChatMessageView] = Field(default_factory=list)


class ChatSessionDetailResponse(ApiResponse[ChatSessionDetailView]):
    pass


class ChatSessionViewResponse(ApiResponse[ChatSessionView]):
    pass


class ChatMessageData(BaseModel):
    id: str
    message: str = "操作成功"


class ChatMessageResponse(ApiResponse[ChatMessageData]):
    pass
