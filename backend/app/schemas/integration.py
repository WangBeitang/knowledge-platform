"""RAG 集成 DTO（仅管理员）：状态查询、Dataset 初始化、任务查询（《API 接口设计》§6）。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.common import ApiResponse

INTEGRATION_SCHEMA_CONFIG = ConfigDict(extra="forbid")


class BootstrapRequest(BaseModel):
    model_config = INTEGRATION_SCHEMA_CONFIG

    verify_only: bool = False


class RagDatasetStatusItem(BaseModel):
    """单档 Dataset 的状态视图。"""

    scope: str
    dataset_id: str
    status: str  # exists | missing | created | existed | verified | failed
    member_status: str  # ensured | verified | missing | failed | skipped
    document_count: int | None = None
    message: str = ""


class RagStatusData(BaseModel):
    import_base_url_configured: bool
    datasets: list[RagDatasetStatusItem]
    overall: str = "ok"  # ok | partial | failed


class RagStatusResponse(ApiResponse[RagStatusData]):
    pass


class BootstrapData(BaseModel):
    verify_only: bool
    datasets: list[RagDatasetStatusItem]
    overall: str = "succeeded"  # succeeded | partial | failed


class BootstrapResponse(ApiResponse[BootstrapData]):
    pass


class IntegrationTaskView(BaseModel):
    """平台任务视图（前端轮询平台 task_id，不接触上游 ID）。"""

    id: str
    operation: str
    status: str
    document_id: str | None = None
    rag_status: str | None = None
    done_nodes: list[Any] = []
    running_nodes: list[Any] = []
    failed_node: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class IntegrationTaskResponse(ApiResponse[IntegrationTaskView]):
    pass
