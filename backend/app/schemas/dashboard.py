"""运营看板 DTO（仅管理员）（《API 接口设计》§13.2）。

冻结口径（不自行扩展）：
- 数据唯一事实来源 = qa_access_logs，不做任何物化/估算；
- PV = 区间内访问日志条数；UV = 独立用户身份数（内部 user_id 与外部
  external_subject_hash 合并去重）；问答量 = 成功轮次数（status=succeeded）；
- success_rate / avg_latency_ms 为空区间时为 null（0/0 无意义，不伪造 0）；
- Token 只使用真实回填（非 NULL）值累加；对应字段全为 NULL 时返回 null，
  缺失行不补 0；token_coverage_rate = 有完整 total_tokens 的日志条数 / 总条数，
  空区间为 null；
- trends 按 UTC 日/小时聚合，只返回真实存在日志的桶，不填充空桶；
- top-questions 按 normalized_question_hash 聚合真实问题日志；
- top-documents 基于真实 citation_document_ids_json 展开计数。
"""

from pydantic import BaseModel

from app.schemas.common import ApiResponse

# 排行 limit 冻结上限（《API 接口设计》§13.2）
DASHBOARD_RANK_LIMIT_MAX = 100


class DashboardSummaryData(BaseModel):
    pv_count: int
    uv_count: int
    question_count: int
    success_rate: float | None
    avg_latency_ms: float | None
    token_input_total: int | None
    token_output_total: int | None
    token_total: int | None
    token_coverage_rate: float | None


class DashboardSummaryResponse(ApiResponse[DashboardSummaryData]):
    pass


class DashboardTrendItem(BaseModel):
    bucket: str
    pv_count: int
    uv_count: int
    question_count: int
    success_rate: float | None
    avg_latency_ms: float | None
    token_total: int | None
    token_coverage_rate: float | None


class DashboardTrendsData(BaseModel):
    granularity: str
    items: list[DashboardTrendItem]


class DashboardTrendsResponse(ApiResponse[DashboardTrendsData]):
    pass


class TopQuestionItem(BaseModel):
    normalized_question: str
    sample_question: str | None
    ask_count: int


class TopQuestionResponse(ApiResponse[list[TopQuestionItem]]):
    pass


class TopDocumentItem(BaseModel):
    document_id: str
    file_name: str | None
    citation_count: int


class TopDocumentResponse(ApiResponse[list[TopDocumentItem]]):
    pass
