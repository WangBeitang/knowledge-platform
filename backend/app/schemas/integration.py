"""RAG 集成 DTO（仅管理员）：状态查询与 Dataset 初始化（《API 接口设计》§6）。"""

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
