"""FAQ 管理 DTO（仅管理员）：候选、正式 FAQ、同步记录（《API 接口设计》§12）。

Schema 一律 extra="forbid"，拒绝未声明字段；
外部请求 Schema 不含 knowledge_scope/dataset 字段（scope 仅由 admin 管理接口显式声明）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import ApiResponse, Page

FAQ_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class FaqCandidateView(BaseModel):
    id: str
    knowledge_scope: str
    normalized_question: str
    normalized_question_hash: str
    sample_questions: list[str]
    ask_count: int
    suggested_answer: str | None = None
    status: str
    published_faq_id: str | None = None
    generated_at: datetime | None = None
    reviewed_by_user_id: str | None = None
    reviewed_at: datetime | None = None


class FaqPublishRequest(BaseModel):
    """候选审核发布：scope + 标准问题 + 标准答案（冻结 §12 发布请求）。"""

    model_config = FAQ_SCHEMA_CONFIG

    knowledge_scope: str
    question: str
    answer: str


class FaqCreateRequest(BaseModel):
    """直接创建并发布（POST /admin/faqs）。"""

    model_config = FAQ_SCHEMA_CONFIG

    knowledge_scope: str
    question: str
    answer: str


class FaqUpdateRequest(BaseModel):
    """更新完整可变字段（PATCH /admin/faqs/{id}）。"""

    model_config = FAQ_SCHEMA_CONFIG

    question: str
    answer: str


class FaqView(BaseModel):
    id: str
    knowledge_scope: str
    question: str
    normalized_question: str
    normalized_question_hash: str
    answer: str
    status: str
    source_candidate_id: str | None = None
    hit_count: int
    rag_sync_status: str
    rag_sync_error: str | None = None
    created_by_user_id: str
    reviewed_by_user_id: str
    published_at: datetime | None = None
    updated_at: datetime | None = None
    unpublished_at: datetime | None = None


class FaqSyncRunView(BaseModel):
    id: str
    knowledge_scope: str
    content_hash: str
    generated_file_name: str
    status: str
    rag_task_id: str | None = None
    rag_document_id: str | None = None
    previous_rag_document_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    requested_by_user_id: str
    created_at: datetime | None = None
    finished_at: datetime | None = None


# ---- 响应壳 ----


class AnalyzeData(BaseModel):
    created: int
    updated: int
    skipped_published: int


class FaqCandidateListData(Page[FaqCandidateView]):
    pass


class FaqListData(Page[FaqView]):
    pass


class FaqSyncRunListData(Page[FaqSyncRunView]):
    pass


class AnalyzeResponse(ApiResponse[AnalyzeData]):
    pass


class FaqCandidateListResponse(ApiResponse[FaqCandidateListData]):
    pass


class FaqCandidateDetailResponse(ApiResponse[FaqCandidateView]):
    pass


class FaqListResponse(ApiResponse[FaqListData]):
    pass


class FaqDetailResponse(ApiResponse[FaqView]):
    pass


class FaqSyncRunListResponse(ApiResponse[FaqSyncRunListData]):
    pass


class FaqSyncRunDetailResponse(ApiResponse[FaqSyncRunView]):
    """sync:retry 响应：全局 {request_id, data: FaqSyncRunView} 契约（首轮复核）。"""

    pass
