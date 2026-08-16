"""文档管理 DTO（仅管理员）：导入、列表、详情、重建、替换、删除（《API 接口设计》§7）。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import ApiResponse, ErrorBody, Page

DOCUMENT_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class ManagedDocumentView(BaseModel):
    """平台对原 RAG Document 的轻量映射视图（不含 file_path/local_dir 等内部路径）。"""

    id: str
    rag_document_id: str
    rag_dataset_id: str
    knowledge_scope: str
    file_name: str
    source_kind: str
    index_version: int
    rag_status: str
    rag_parse_status: str | None = None
    rag_index_status: str | None = None
    platform_status: str
    chunk_count: int
    latest_rag_task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentImportItem(BaseModel):
    """单个文件提交结果：pending（已提交上游）/ rejected（请求级拒绝）。"""

    file_name: str
    document_id: str | None = None
    task_id: str | None = None
    status: str  # pending | rejected
    error: ErrorBody | None = None


class DocumentImportData(BaseModel):
    knowledge_scope: str
    submitted_count: int
    rejected_count: int
    items: list[DocumentImportItem]


class DocumentImportResponse(ApiResponse[DocumentImportData]):
    pass


class DocumentListData(Page[ManagedDocumentView]):
    pass


class DocumentListResponse(ApiResponse[DocumentListData]):
    pass


class DocumentDetailResponse(ApiResponse[ManagedDocumentView]):
    pass


class RebuildData(BaseModel):
    task_id: str
    document_id: str
    operation: str = "document_rebuild"
    status: str = "pending"


class RebuildResponse(ApiResponse[RebuildData]):
    pass


class ReplaceData(BaseModel):
    task_id: str
    new_document_id: str
    replacement_id: str
    status: str = "pending"


class ReplaceResponse(ApiResponse[ReplaceData]):
    pass


class DeleteData(BaseModel):
    id: str
    platform_status: str = "deleted"


class DeleteResponse(ApiResponse[DeleteData]):
    pass
