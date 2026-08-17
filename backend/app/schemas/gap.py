"""知识缺口管理 DTO（仅管理员）：候选列表 / ignore / resolve（《API 接口设计》§13.1）。

Schema 一律 extra="forbid"，拒绝未声明字段；
resolve 仅支持冻结字段 resolution_note / resolved_document_id，不自行扩展状态。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApiResponse, Page

GAP_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class GapView(BaseModel):
    id: str
    knowledge_scope: str
    normalized_question: str
    normalized_question_hash: str
    sample_questions: list[str]
    ask_count: int
    reason_code: str
    status: str
    resolution_note: str | None = None
    resolved_document_id: str | None = None
    reviewed_by_user_id: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    reviewed_at: datetime | None = None


class GapResolveRequest(BaseModel):
    """标记已处理：可选管理说明 + 可选关联平台文档 ID（冻结字段，不扩展）。"""

    model_config = GAP_SCHEMA_CONFIG

    resolution_note: str | None = Field(default=None, max_length=1000)
    resolved_document_id: str | None = Field(default=None, max_length=36)


# ---- 响应壳 ----


class GapAnalyzeData(BaseModel):
    created: int
    updated: int


class GapListData(Page[GapView]):
    pass


class GapAnalyzeResponse(ApiResponse[GapAnalyzeData]):
    pass


class GapListResponse(ApiResponse[GapListData]):
    pass


class GapDetailResponse(ApiResponse[GapView]):
    pass
