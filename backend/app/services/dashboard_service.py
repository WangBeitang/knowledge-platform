"""运营看板服务：summary / trends / top-questions / top-documents（仅管理员）。

冻结口径（《API 接口设计》§13.2 / 模块 SPEC §4 dashboard.py）：
- 数据唯一事实来源 = qa_access_logs 真实业务日志，不生成模拟统计；
- 无物化统计 Worker / ES / ClickHouse，V1 直接基于现有 MySQL 查询；
- Token 只使用真实回填数据；缺失值保持 null 语义，不估算、不补 0；
- 通用过滤 date_from/date_to/channel；排行 limit <= 100。
"""

from app.repositories.managed_document_repository import ManagedDocumentRepository
from app.repositories.qa_access_log_repository import QaAccessLogRepository
from app.schemas.dashboard import (
    DashboardSummaryData,
    DashboardTrendItem,
    DashboardTrendsData,
    TopDocumentItem,
    TopQuestionItem,
)


class DashboardService:
    def __init__(
        self,
        *,
        logs: QaAccessLogRepository,
        documents: ManagedDocumentRepository,
    ) -> None:
        self.logs = logs
        self.documents = documents

    async def get_summary(
        self,
        *,
        date_from=None,
        date_to=None,
        channel: str | None = None,
    ) -> DashboardSummaryData:
        stat = await self.logs.aggregate_summary(
            date_from=date_from, date_to=date_to, channel=channel
        )
        return DashboardSummaryData(**stat)

    async def get_trends(
        self,
        *,
        date_from=None,
        date_to=None,
        channel: str | None = None,
        granularity: str = "day",
    ) -> DashboardTrendsData:
        items = await self.logs.aggregate_by_time(
            date_from=date_from,
            date_to=date_to,
            channel=channel,
            granularity=granularity,
        )
        return DashboardTrendsData(
            granularity=granularity,
            items=[DashboardTrendItem(**item) for item in items],
        )

    async def get_top_questions(
        self,
        *,
        date_from=None,
        date_to=None,
        channel: str | None = None,
        limit: int = 10,
    ) -> list[TopQuestionItem]:
        items = await self.logs.aggregate_by_question(
            date_from=date_from, date_to=date_to, channel=channel, limit=limit
        )
        return [TopQuestionItem(**item) for item in items]

    async def get_top_documents(
        self,
        *,
        date_from=None,
        date_to=None,
        channel: str | None = None,
        limit: int = 10,
    ) -> list[TopDocumentItem]:
        items = await self.logs.aggregate_by_citation(
            date_from=date_from, date_to=date_to, channel=channel, limit=limit
        )
        views: list[TopDocumentItem] = []
        for item in items:
            doc = await self.documents.get_by_rag_document_id(item["document_id"])
            views.append(
                TopDocumentItem(
                    document_id=item["document_id"],
                    file_name=doc.file_name if doc else None,
                    citation_count=item["citation_count"],
                )
            )
        return views
