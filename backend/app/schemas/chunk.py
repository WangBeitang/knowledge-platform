"""Chunk 管理 DTO（仅管理员）：分页列表、详情、启停（《API 接口设计》§8）。

禁止 Chunk 正文编辑接口（无 update_text / PATCH text）。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.common import ApiResponse

CHUNK_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class ChunkView(BaseModel):
    """平台 Chunk 视图（Adapter 已翻译字段；正文不进 MySQL）。"""

    chunk_id: str
    document_id: str  # 平台 document id
    index_version: int
    position: int  # ← chunk_index
    text: str  # 列表=content_preview；详情=content
    enabled: bool  # ← effective_enabled
    disabled_reason_code: str | None = None  # ← latest_event.reason_type
    disabled_reason_text: str | None = None  # ← latest_event.reason_detail
    metadata: dict[str, Any] = {}


class ChunkListData(BaseModel):
    items: list[ChunkView]
    total: int
    page: int
    page_size: int


class ChunkListResponse(ApiResponse[ChunkListData]):
    pass


class ChunkDetailResponse(ApiResponse[ChunkView]):
    pass


class ChunkSetEnabledRequest(BaseModel):
    model_config = CHUNK_SCHEMA_CONFIG

    enabled: bool
    reason_code: str  # parse_error|header_footer|garbled_text|outdated_content|
    #                # human_misjudgment|manual_restore|other
    reason_text: str = ""
    expected_index_version: int


class ChunkSetEnabledData(BaseModel):
    document_id: str
    chunk_id: str
    index_version: int
    enabled: bool


class ChunkSetEnabledResponse(ApiResponse[ChunkSetEnabledData]):
    pass
